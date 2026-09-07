#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SIP 线路测试台 (sip-tester)

本地网页拨号面板 + baresip 原生 SIP 栈：
  - 浏览器只做控制面板（拨号 / 挂断 / DTMF / 看质量指标）
  - 真正的 SIP 注册、RTP 语音走本机 baresip，音频用 macOS 默认麦克风 / 扬声器
  - 质量指标：对端 RTCP（RTT / 抖动 / 丢包）+ 本地抖动缓冲统计（丢包 / 乱序 / 迟到）+ 建立时延

启动:  python3 server.py [--port 8790] [--no-open]
依赖:  brew install baresip   （Python 仅用标准库）
"""
import argparse
import json
import os
import pty
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty, Full, Queue
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE, 'static')
RUNTIME_DIR = os.path.join(BASE, 'runtime')
HISTORY_FILE = os.path.join(RUNTIME_DIR, 'history.json')
CTRL_ADDR = ('127.0.0.1', 4490)
LOG_KEEP = 600
HISTORY_KEEP = 200
POLL_INTERVAL = 2.0
BASE_MODULES = ['g711', 'uuid', 'account', 'menu', 'ctrl_tcp', 'coreaudio', 'rtcpsummary']

RE_SUMMARY = re.compile(
    r'EX=BareSip;CS=(-?\d+);CD=(-?\d+);PR=(\d+);PS=(\d+);PL=(-?\d+),(-?\d+);'
    r'PD=(-?\d+),(-?\d+);JI=([\d.]+),([\d.]+);DL=([\d.]+);IP=([^,;]*),([^;]*);')
RE_PACKETS = re.compile(r'^packets:\s+(\d+)\s+(\d+)')
RE_ERRORS = re.compile(r'^errors:\s+(-?\d+)\s+(-?\d+)')
RE_MEMBER = re.compile(r'member 0x[0-9a-fA-F]+: lost=(-?\d+) Jitter=([\d.]+)ms RTT=([\d.]+)ms')
RE_RCVD = re.compile(r'psent=(\d+) rcvd=(\d+)')
RE_TXP = re.compile(r'TX: packets=(\d+)')
RE_ENC = re.compile(r'encode:\s*(\S+)')
RE_DEC = re.compile(r'decode:\s*(\S+)')


def find_baresip():
    exe = shutil.which('baresip') or '/opt/homebrew/bin/baresip'
    if not os.path.isfile(exe):
        return None, None
    real = os.path.realpath(exe)
    for base in (os.path.dirname(os.path.dirname(real)), os.path.dirname(os.path.dirname(exe))):
        d = os.path.join(base, 'lib', 'baresip', 'modules')
        if os.path.isdir(d):
            return exe, d
    return exe, None


def netstring(payload):
    return str(len(payload)).encode() + b':' + payload + b','


def parse_netstrings(buf):
    """从 bytearray 中切出完整的 netstring payload（原地消费）"""
    out = []
    while True:
        i = buf.find(b':')
        if i < 0:
            break
        try:
            n = int(buf[:i])
        except ValueError:
            buf.clear()
            break
        end = i + 1 + n
        if len(buf) < end + 1:
            break
        out.append(bytes(buf[i + 1:end]))
        del buf[:end + 1]
    return out


class Ctrl:
    """baresip ctrl_tcp 客户端：netstring 封装的 JSON 命令 / 响应 / 事件"""

    def __init__(self, addr, on_event, on_close):
        self.addr = addr
        self.on_event = on_event
        self.on_close = on_close
        self.sock = None
        self.pending = {}
        self.lock = threading.Lock()
        self.alive = False

    def connect(self, timeout, still_alive):
        deadline = time.time() + timeout
        while time.time() < deadline and still_alive():
            try:
                s = socket.create_connection(self.addr, timeout=2)
            except OSError:
                time.sleep(0.25)
                continue
            s.settimeout(None)
            self.sock = s
            self.alive = True
            threading.Thread(target=self._reader, daemon=True).start()
            return True
        return False

    def _reader(self):
        buf = bytearray()
        try:
            while True:
                data = self.sock.recv(65536)
                if not data:
                    break
                buf.extend(data)
                for payload in parse_netstrings(buf):
                    try:
                        msg = json.loads(payload.decode('utf-8', 'replace'))
                    except ValueError:
                        continue
                    if msg.get('response'):
                        with self.lock:
                            slot = self.pending.get(msg.get('token'))
                        if slot:
                            slot[1] = msg
                            slot[0].set()
                    elif msg.get('event'):
                        self.on_event(msg)
        except OSError:
            pass
        finally:
            self.alive = False
            with self.lock:
                for slot in self.pending.values():
                    slot[0].set()
            self.on_close()

    def send(self, command, params='', timeout=5.0):
        if not self.alive:
            return {'ok': False, 'data': 'baresip 未连接'}
        tok = uuid.uuid4().hex[:8]
        slot = [threading.Event(), None]
        with self.lock:
            self.pending[tok] = slot
        try:
            msg = {'command': command, 'token': tok}
            if params:
                msg['params'] = params
            self.sock.sendall(netstring(json.dumps(msg).encode()))
            slot[0].wait(timeout)
        except OSError as e:
            return {'ok': False, 'data': str(e)}
        finally:
            with self.lock:
                self.pending.pop(tok, None)
        if slot[1] is None:
            return {'ok': False, 'data': '命令超时: %s' % command}
        return {'ok': bool(slot[1].get('ok')), 'data': slot[1].get('data', '') or ''}

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class App:
    def __init__(self):
        self.exe, self.modules_dir = find_baresip()
        self.available = set()
        if self.modules_dir:
            self.available = set(f[:-3] for f in os.listdir(self.modules_dir) if f.endswith('.so'))
        self.lock = threading.RLock()
        self.proc = None
        self.ctrl = None
        self.cfg = None
        self.gen = 0
        self.subs = set()
        self.log = deque(maxlen=LOG_KEEP)
        self.history = self._load_history()
        self._local_hangup = False
        self._dial_ts = None
        self._dial_number = None
        self.state = {}
        self.reset_state()

    # ---------- 状态 / 广播 ----------
    def reset_state(self):
        self.state = {
            'running': False, 'account': None,
            'reg': {'status': 'idle', 'detail': '', 'ts': None},
            'call': None, 'stats': None, 'diag': '',
        }

    def snapshot(self):
        d = dict(self.state)
        d['baresip'] = self.exe
        d['modules_dir'] = self.modules_dir
        d['ctrl'] = bool(self.ctrl and self.ctrl.alive)
        d['now'] = time.time()
        return d

    def subscribe(self):
        q = Queue(maxsize=1000)
        with self.lock:
            self.subs.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)

    def emit(self, kind, data):
        payload = 'event: %s\ndata: %s\n\n' % (kind, json.dumps(data, ensure_ascii=False))
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(payload)
            except Full:
                pass

    def push_state(self):
        self.emit('state', self.snapshot())

    def add_log(self, line, kind='log'):
        item = {'ts': time.time(), 'line': line, 'kind': kind}
        self.log.append(item)
        self.emit('log', item)

    # ---------- 历史 ----------
    def _load_history(self):
        try:
            with open(HISTORY_FILE, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []

    def _save_history(self):
        try:
            os.makedirs(RUNTIME_DIR, exist_ok=True)
            with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.history, f, ensure_ascii=False)
        except Exception as e:
            self.add_log('保存历史失败: %s' % e, 'err')

    def clear_history(self):
        with self.lock:
            self.history = []
            self._save_history()
        self.emit('history', self.history)

    # ---------- 配置生成 ----------
    def build_account_line(self, cfg):
        transport = cfg.get('transport') or 'udp'
        server = cfg['server'].strip()
        user = cfg['user'].strip()
        domain = (cfg.get('domain') or '').strip()
        host = domain or server
        acc = ['<sip:%s@%s;transport=%s>' % (user, host, transport)]
        if (cfg.get('auth_user') or '').strip():
            acc.append('auth_user=%s' % cfg['auth_user'].strip())
        if cfg.get('password'):
            acc.append('auth_pass=%s' % cfg['password'])
        if domain and domain != server:
            acc.append('outbound="sip:%s;transport=%s"' % (server, transport))
        regint = int(cfg.get('regint') or 300) if cfg.get('register', True) else 0
        acc.append('regint=%d' % regint)
        acc.append('answermode=manual')
        codecs = {'pcmu': 'PCMU/8000/1,PCMA/8000/1', 'pcma_only': 'PCMA/8000/1',
                  'pcmu_only': 'PCMU/8000/1'}.get(cfg.get('codec'), 'PCMA/8000/1,PCMU/8000/1')
        acc.append('audio_codecs=%s' % codecs)
        acc.append('ptime=20')
        if cfg.get('stun'):
            acc.append('stunserver=stun:%s' % ((cfg.get('stun_server') or '').strip() or 'stun.l.google.com:19302'))
            acc.append('medianat=stun')
        return ';'.join(acc)

    def write_conf(self, cfg):
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        mods = list(BASE_MODULES)
        if cfg.get('stun'):
            mods.append('stun')
        missing = [m for m in ('ctrl_tcp', 'coreaudio', 'g711', 'account', 'menu') if m not in self.available]
        if missing:
            raise RuntimeError('baresip 缺少模块: %s' % ', '.join(missing))
        lines = ['module_path %s' % self.modules_dir]
        lines += ['module %s.so' % m for m in mods if m in self.available]
        # 测试用: SIP_TESTER_AUDIO="ausine,440;aufile,/tmp/rx.wav" 可替换 音源;播放器 (免麦克风)
        audio_src, audio_play = 'coreaudio', 'coreaudio'
        override = os.environ.get('SIP_TESTER_AUDIO')
        if override and ';' in override:
            audio_src, audio_play = override.split(';', 1)
            for drv in (audio_src.split(',')[0], audio_play.split(',')[0]):
                if drv in self.available and drv not in mods:
                    mods.append(drv)
            lines += ['module %s.so' % m for m in mods if m in self.available and ('module %s.so' % m) not in lines]
        lines += [
            'ctrl_tcp_listen %s:%d' % CTRL_ADDR,
            'audio_player %s' % audio_play,
            'audio_source %s' % audio_src,
            'audio_alert coreaudio',
            'rtp_stats yes',
            'rtp_timeout 60',
            'call_max_calls 1',
            'sip_cafile /etc/ssl/cert.pem',
            'sip_verify_server no',
            'statmode_default off',
        ]
        if (cfg.get('net_interface') or '').strip():
            lines.append('net_interface %s' % cfg['net_interface'].strip())
        with open(os.path.join(RUNTIME_DIR, 'config'), 'w') as f:
            f.write('\n'.join(lines) + '\n')
        with open(os.path.join(RUNTIME_DIR, 'accounts'), 'w') as f:
            f.write(self.build_account_line(cfg) + '\n')

    # ---------- 进程生命周期 ----------
    def start(self, cfg):
        if not self.exe:
            return {'ok': False, 'error': '未找到 baresip，请先执行: brew install baresip'}
        if not (cfg.get('server') or '').strip() or not (cfg.get('user') or '').strip():
            return {'ok': False, 'error': '服务器地址和用户名必填'}
        self.stop()
        with self.lock:
            try:
                self.write_conf(cfg)
            except Exception as e:
                return {'ok': False, 'error': str(e)}
            self.cfg = cfg
            self.gen += 1
            gen = self.gen
            self.log.clear()
            self._kill_stale()
            self.reset_state()
            master, slave = pty.openpty()
            cmd = [self.exe, '-f', RUNTIME_DIR, '-c']
            if cfg.get('sip_trace'):
                cmd.append('-s')
            try:
                self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=slave,
                                             stderr=slave, close_fds=True)
            except OSError as e:
                os.close(master)
                os.close(slave)
                return {'ok': False, 'error': '启动 baresip 失败: %s' % e}
            os.close(slave)
            threading.Thread(target=self._read_output, args=(master, gen), daemon=True).start()
            self.state['running'] = True
            self.state['account'] = {
                'aor': self.build_account_line(cfg).split(';')[0].strip('<'),
                'server': cfg['server'].strip(), 'transport': cfg.get('transport') or 'udp',
                'register': bool(cfg.get('register', True)),
            }
            self.state['reg'] = {'status': 'registering' if cfg.get('register', True) else 'none',
                                 'detail': '', 'ts': time.time()}
            self.add_log('启动: %s' % ' '.join(cmd), 'sys')
            self.add_log('账号: %s' % re.sub(r'auth_pass=[^;]*', 'auth_pass=***', self.build_account_line(cfg)), 'sys')
        self.push_state()

        ctrl = Ctrl(CTRL_ADDR, self.on_event, lambda: self._on_ctrl_closed(gen))
        if not ctrl.connect(10.0, lambda: self.proc is not None and self.proc.poll() is None):
            tail = [x['line'] for x in list(self.log)[-15:]]
            self.stop()
            return {'ok': False, 'error': 'baresip 启动失败或控制端口连不上', 'log': tail}
        with self.lock:
            self.ctrl = ctrl
        threading.Thread(target=self._poller, args=(gen,), daemon=True).start()
        self.push_state()
        return {'ok': True}

    def _kill_stale(self):
        """清理上次异常退出遗留的 baresip（否则它还占着控制端口，新进程连不上）"""
        prefix = '%s -f %s' % (self.exe, RUNTIME_DIR)
        try:
            out = subprocess.run(['ps', '-axo', 'pid=,command='], stdout=subprocess.PIPE,
                                 universal_newlines=True).stdout
        except OSError:
            return
        killed = False
        for line in out.splitlines():
            pid, _, cmd = line.strip().partition(' ')
            if cmd.strip().startswith(prefix) and pid.isdigit():
                try:
                    os.kill(int(pid), signal.SIGTERM)
                    killed = True
                except OSError:
                    pass
        if killed:
            time.sleep(0.5)

    def stop(self):
        with self.lock:
            proc, ctrl = self.proc, self.ctrl
            self.proc, self.ctrl = None, None
            self.gen += 1
        if ctrl:
            if ctrl.alive:
                ctrl.send('quit', timeout=2.0)
            ctrl.close()
        if proc and proc.poll() is None:
            try:
                proc.wait(3.0)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(3.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
        with self.lock:
            was_running = self.state.get('running')
            self.reset_state()
        if was_running:
            self.add_log('baresip 已停止', 'sys')
        self.push_state()
        return {'ok': True}

    def _on_ctrl_closed(self, gen):
        with self.lock:
            if gen != self.gen:
                return
            self.state['running'] = False
            self.state['reg'] = {'status': 'idle', 'detail': '控制连接断开', 'ts': time.time()}
        self.add_log('baresip 控制连接断开', 'err')
        self.push_state()

    def _read_output(self, master, gen):
        buf = b''
        while True:
            try:
                data = os.read(master, 4096)
            except OSError:
                break
            if not data:
                break
            buf += data
            while True:
                m = re.search(rb'[\r\n]', buf)
                if not m:
                    break
                line, buf = buf[:m.start()], buf[m.end():]
                self._handle_line(line.decode('utf-8', 'replace'), gen)
        try:
            os.close(master)
        except OSError:
            pass
        with self.lock:
            if gen != self.gen:
                return
            rc = self.proc.poll() if self.proc else None
            self.state['running'] = False
            self.state['reg'] = {'status': 'idle', 'detail': 'baresip 已退出', 'ts': time.time()}
        self.add_log('baresip 已退出 (code=%s)' % rc, 'err')
        self.push_state()

    # ---------- baresip 输出解析 ----------
    def _handle_line(self, line, gen):
        if gen != self.gen or not line.strip():
            return
        kind = 'log'
        low = line.lower()
        if ('warning' in low or 'error' in low or 'failed' in low) and not low.startswith('errors:'):
            kind = 'warn'
        self.add_log(line, kind)
        m = RE_SUMMARY.search(line)
        if m:
            g = m.groups()
            setup_ms = int(g[0])
            if setup_ms < 0 or setup_ms > 3600000:
                setup_ms = None  # 未接通时 baresip 给的是无效值
            self._merge_call({'summary': {
                'setup_ms': setup_ms, 'duration_s': int(g[1]),
                'rx_packets': int(g[2]), 'tx_packets': int(g[3]),
                'rx_lost': int(g[4]), 'tx_lost': int(g[5]),
                'rx_discard': int(g[6]), 'tx_discard': int(g[7]),
                'rx_jit_ms': float(g[8]), 'tx_jit_ms': float(g[9]),
                'rtt_ms': float(g[10]), 'local': g[11], 'remote': g[12],
            }})
            return
        if 'EX=BareSip;ERROR=No RTCP stats collected' in line:
            self._merge_call({'summary': {'no_rtcp': True}})
            return
        m = RE_PACKETS.match(line)
        if m:
            self._merge_call({'rtp': {'tx_packets': int(m.group(1)), 'rx_packets': int(m.group(2))}})
            return
        m = RE_ERRORS.match(line)
        if m:
            self._merge_call({'rtp_err': {'tx': int(m.group(1)), 'rx': int(m.group(2))}})

    def _merge_call(self, patch):
        """把通话结束后才打印出来的统计并入最近一通电话记录"""
        with self.lock:
            c = self.state.get('call')
            if not c:
                return
            for k, v in patch.items():
                if isinstance(v, dict) and isinstance(c.get(k), dict):
                    c[k].update(v)
                else:
                    c[k] = v
            if c.get('state') == 'closed':
                self._save_history()
        self.push_state()
        self.emit('history', self.history)

    def _parse_diag(self, text):
        stats = {}
        m = RE_MEMBER.search(text)
        if m:
            local = {'lost': int(m.group(1)), 'jit_ms': float(m.group(2)), 'rtt_ms': float(m.group(3))}
            m2 = RE_RCVD.search(text)
            if m2:
                local['psent'] = int(m2.group(1))
                local['rcvd'] = int(m2.group(2))
                total = local['rcvd'] + max(0, local['lost'])
                local['lost_pct'] = round(100.0 * max(0, local['lost']) / total, 2) if total else 0.0
            m3 = RE_TXP.search(text)
            if m3:
                local['tx_packets'] = int(m3.group(1))
            stats['local'] = local
        enc, dec = RE_ENC.search(text), RE_DEC.search(text)
        if enc or dec:
            stats['codec'] = {'enc': enc.group(1) if enc else '', 'dec': dec.group(1) if dec else ''}
        with self.lock:
            cur = self.state.get('stats') or {}
            cur.update(stats)
            cur['ts'] = time.time()
            self.state['stats'] = cur
            self.state['diag'] = text
        self.push_state()

    def _poller(self, gen):
        while True:
            time.sleep(POLL_INTERVAL)
            with self.lock:
                if gen != self.gen or not self.ctrl or not self.ctrl.alive:
                    return
                c = self.state.get('call')
                active = bool(c and c.get('state') in ('progress', 'answered', 'established'))
                ctrl = self.ctrl
            if not active:
                continue
            r = ctrl.send('audio_debug', timeout=3.0)
            if r['ok'] and r['data']:
                self._parse_diag(r['data'])

    # ---------- 事件 ----------
    def _new_call(self, ev, direction, now):
        return {
            'id': ev.get('id'), 'peer': ev.get('peeruri') or '', 'number': self._dial_number or '',
            'dir': direction, 'state': 'calling' if direction == 'out' else 'incoming',
            't_start': self._dial_ts or now, 't_ring': None, 't_est': None, 't_rtp': None, 't_end': None,
            'early_media': False, 'reason': '', 'local_hangup': False,
            'pdd_ms': None, 'answer_ms': None, 'duration_s': None,
            'stats': None, 'summary': None, 'rtp': None,
        }

    def on_event(self, ev):
        t = ev.get('type', '')
        prm = ev.get('param', '') or ''
        now = time.time()
        self.add_log('[事件] %s %s %s' % (t, ev.get('peeruri', ''), prm), 'event')
        with self.lock:
            st = self.state
            c = st.get('call')
            active = c if (c and c.get('state') != 'closed') else None
            if t == 'REGISTERING':
                st['reg'] = {'status': 'registering', 'detail': prm, 'ts': now}
            elif t == 'REGISTER_OK':
                st['reg'] = {'status': 'ok', 'detail': prm, 'ts': now}
            elif t == 'REGISTER_FAIL':
                st['reg'] = {'status': 'fail', 'detail': prm, 'ts': now}
            elif t == 'UNREGISTERING':
                st['reg'] = {'status': 'unregistering', 'detail': prm, 'ts': now}
            elif t == 'CALL_OUTGOING':
                if not active:
                    st['call'] = self._new_call(ev, 'out', now)
                    st['stats'] = None
                    st['diag'] = ''
                else:
                    active['id'] = active.get('id') or ev.get('id')
                    active['peer'] = active.get('peer') or ev.get('peeruri') or ''
            elif t == 'CALL_INCOMING':
                if not active:
                    self._dial_ts = None
                    self._dial_number = None
                    st['call'] = self._new_call(ev, 'in', now)
                    st['stats'] = None
                    st['diag'] = ''
            elif active and t in ('CALL_RINGING', 'CALL_PROGRESS'):
                active['state'] = 'ringing' if t == 'CALL_RINGING' else 'progress'
                if t == 'CALL_PROGRESS':
                    active['early_media'] = True
                if active['t_ring'] is None:
                    active['t_ring'] = now
                    active['pdd_ms'] = int((now - active['t_start']) * 1000)
            elif active and t == 'CALL_ANSWERED':
                active['state'] = 'answered'
            elif active and t == 'CALL_ESTABLISHED':
                active['state'] = 'established'
                if active['t_est'] is None:
                    active['t_est'] = now
                    active['answer_ms'] = int((now - active['t_start']) * 1000)
            elif active and t == 'CALL_RTPESTAB':
                if active['t_rtp'] is None:
                    active['t_rtp'] = now
            elif t == 'CALL_RTCP':
                rs = ev.get('rtcp_stats')
                if rs and active:
                    cur = st.get('stats') or {}
                    cur['rtcp'] = {
                        'ts': now, 'rtt_ms': rs.get('rtt', 0) / 1000.0,
                        'tx_sent': rs['tx'].get('sent'), 'tx_lost': rs['tx'].get('lost'),
                        'tx_jit_ms': rs['tx'].get('jit', 0) / 1000.0,
                        'rx_sent': rs['rx'].get('sent'), 'rx_lost': rs['rx'].get('lost'),
                        'rx_jit_ms': rs['rx'].get('jit', 0) / 1000.0,
                    }
                    cur['ts'] = now
                    st['stats'] = cur
            elif t == 'CALL_CLOSED':
                if active and (not ev.get('id') or not active.get('id') or ev.get('id') == active.get('id')):
                    active['state'] = 'closed'
                    active['t_end'] = now
                    active['reason'] = prm
                    active['local_hangup'] = self._local_hangup
                    if active.get('t_est'):
                        active['duration_s'] = round(now - active['t_est'], 1)
                    active['stats'] = st.get('stats')
                    self.history.insert(0, active)
                    del self.history[HISTORY_KEEP:]
                    self._save_history()
                    self._local_hangup = False
                    self._dial_ts = None
                    self._dial_number = None
            elif t in ('EXIT', 'SHUTDOWN'):
                st['running'] = False
        self.push_state()
        if t == 'CALL_CLOSED':
            self.emit('history', self.history)

    # ---------- 动作 ----------
    def _require_ctrl(self):
        with self.lock:
            ctrl = self.ctrl
        if not ctrl or not ctrl.alive:
            return None, {'ok': False, 'error': '尚未连接 SIP 服务'}
        return ctrl, None

    def dial(self, number):
        ctrl, err = self._require_ctrl()
        if err:
            return err
        number = re.sub(r'[\s\-()]', '', number or '')
        if not number:
            return {'ok': False, 'error': '号码为空'}
        with self.lock:
            c = self.state.get('call')
            if c and c.get('state') != 'closed':
                return {'ok': False, 'error': '当前有进行中的通话'}
            self._dial_ts = time.time()
            self._dial_number = number
            self._local_hangup = False
        r = ctrl.send('dial', number)
        if not r['ok']:
            with self.lock:
                self._dial_ts = None
                self._dial_number = None
            msg = r['data'].strip() or '未知错误'
            if 'could not find UA' in msg:
                msg += '\n账号尚未注册成功，baresip 不允许用未注册账号外呼；IP 鉴权线路请取消勾选「注册」后重连'
            return {'ok': False, 'error': '拨号失败: %s' % msg}
        with self.lock:
            c = self.state.get('call')
            if not c or c.get('state') == 'closed':
                self.state['call'] = self._new_call({'peeruri': number}, 'out', time.time())
                self.state['stats'] = None
                self.state['diag'] = ''
        self.push_state()
        return {'ok': True, 'data': r['data']}

    def hangup(self):
        ctrl, err = self._require_ctrl()
        if err:
            return err
        with self.lock:
            self._local_hangup = True
        r = ctrl.send('hangup')
        return {'ok': r['ok'], 'data': r['data']}

    def accept(self):
        ctrl, err = self._require_ctrl()
        if err:
            return err
        r = ctrl.send('accept')
        return {'ok': r['ok'], 'data': r['data']}

    def dtmf(self, digits):
        ctrl, err = self._require_ctrl()
        if err:
            return err
        digits = re.sub(r'[^0-9A-Da-d*#]', '', digits or '')
        if not digits:
            return {'ok': False, 'error': '无效按键'}
        r = ctrl.send('sndcode', digits)
        return {'ok': r['ok'], 'data': r['data']}

    def mute(self, on):
        ctrl, err = self._require_ctrl()
        if err:
            return err
        r = ctrl.send('mute', 'yes' if on else 'no')
        return {'ok': r['ok'], 'data': r['data']}

    def raw(self, command, params):
        ctrl, err = self._require_ctrl()
        if err:
            return err
        return ctrl.send(command, params or '')


app = App()


class Handler(BaseHTTPRequestHandler):
    server_version = 'sip-tester/1.0'

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n) if n else b''
        try:
            return json.loads(raw or b'{}')
        except ValueError:
            return {}

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ('/', '/index.html'):
            try:
                with open(os.path.join(STATIC_DIR, 'index.html'), 'rb') as f:
                    body = f.read()
            except OSError:
                return self._json({'error': 'static/index.html 缺失'}, 500)
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
        elif p == '/api/state':
            self._json(app.snapshot())
        elif p == '/api/history':
            self._json(app.history)
        elif p == '/api/log':
            self._json(list(app.log))
        elif p == '/api/events':
            self._sse()
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        p = urlparse(self.path).path
        b = self._body()
        if p == '/api/start':
            return self._json(app.start(b))
        if p == '/api/stop':
            return self._json(app.stop())
        if p == '/api/dial':
            return self._json(app.dial(b.get('number', '')))
        if p == '/api/hangup':
            return self._json(app.hangup())
        if p == '/api/accept':
            return self._json(app.accept())
        if p == '/api/dtmf':
            return self._json(app.dtmf(b.get('digits', '')))
        if p == '/api/mute':
            return self._json(app.mute(bool(b.get('on'))))
        if p == '/api/cmd':
            return self._json(app.raw(b.get('command', ''), b.get('params', '')))
        if p == '/api/history/clear':
            app.clear_history()
            return self._json({'ok': True})
        self._json({'error': 'not found'}, 404)

    def _sse(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        q = app.subscribe()
        try:
            self.wfile.write(('event: state\ndata: %s\n\n' % json.dumps(app.snapshot(), ensure_ascii=False)).encode())
            self.wfile.flush()
            while True:
                try:
                    item = q.get(timeout=15)
                except Empty:
                    item = ': ping\n\n'
                self.wfile.write(item.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            app.unsubscribe(q)


def main():
    ap = argparse.ArgumentParser(description='SIP 线路测试台')
    ap.add_argument('--port', type=int, default=8790)
    ap.add_argument('--no-open', action='store_true', help='不自动打开浏览器')
    args = ap.parse_args()
    if not app.exe:
        print('未找到 baresip，请先执行: brew install baresip', file=sys.stderr)
    srv = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)

    def _term(signum, frame):
        app.stop()
        os._exit(0)
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGHUP, _term)
    url = 'http://127.0.0.1:%d' % args.port
    print('SIP 线路测试台: %s   (Ctrl+C 退出)' % url)
    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()


if __name__ == '__main__':
    main()
