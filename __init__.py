#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
    irclite - a minimal lightwight IRC client API using gevent


import irclite
client = irclite.Client()

# load the config dict
client.load({
    'nick' : 'MyBot', 'ident' : 'MyBot', 'realname' : 'MyBot', 'debug' : False,
    'networks' : ({
            'name' : 'MyNetwork',
            'host' : 'irc.mynetwork.com',
            'port' : '6667',
            'enabled' : True,
        }),
    'onconnect' : [
        lambda net: net.name == "MyNetwork" and net.join('#Chat'),
    ],
})

client.init() # setup our network greenlets
client.run() # non-blocking waiting for completion



MIT License

Copyright (c) 2013 Fenris_Wolf, YSPStudios

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
import re
import time
import socket
import logging
import random
import gevent

# messages come in 3 formats:
# :nick[!ident@host] TYPE data
# :server.name TYPE data (: prefix maybe excluded)
# TYPE data

RE_TYPE = re.compile(r"^(?:\:(\S+)|\:?([a-z0-9A-Z_-]+\.[a-z0-9A-Z_\.-]+))$")
RE_SENDER = re.compile(r"^\:?([^\!]+)\!([^\@]+)\@(\S+)$")
RE_CTCP = re.compile(r"\001(\S+)(?: +(.+))?\001")

# connection state flags
OFFLINE = 0
CONNECTING = 1
ONLINE = 2

logger = logging.getLogger(__name__)

class Event(object):
    """Class representing a IRC event
    """
    __slots__ = ('network', 'text', 'fulltext', 'dest', 'type', 'ctcp', 'source',
                 'nick', 'ident', 'host', 'chan', 'data')

    def __init__(self, network, data):
        self.text = None
        self.fulltext = None
        self.dest = None
        self.type = None
        self.ctcp = None
        self.source = None
        self.nick = None
        self.ident = None
        self.host = None
        self.chan = None

        self.network = network
        self.data = data
        tokens = data.split()

        # identify msg format
        match = RE_TYPE.match(tokens[0])
        if match:
            self.source = match.group(1) or match.group(2)
            self.type = tokens[1]

        else:
            self.type = tokens[0]

        match = re.search(r"\:(.*)$", data[1:])
        if match:
            self.text = match.group(1)

        # for any msg with 3+ tokens that has the msg type identifed in 2nd token
        # the 3rd token (index 2) is a destination for the msg
        if len(tokens) > 2 and self.type == tokens[1]:
            self.dest = tokens[2]
            if self.dest.startswith('#'):
                self.chan = self.dest

            if len(tokens) > 3:
                self.fulltext = data.split(None, 3)[-1]

        if self.source:
            match = RE_SENDER.match(self.source)
            if match:
                self.nick = match.group(1)
                self.ident = match.group(2)
                self.host = match.group(3)

        # convert any '001' style numerics to '1' for consistancy across ircds
        numeric = self.as_numeric()
        if numeric:
            self.type = str(numeric)


    def reply(self, text):
        if self.type != 'PRIVMSG':
            return # TODO: raise exception

        if self.chan:
            self.network.privmsg(self.chan, text)

        else:
            self.network.privmsg(self.source, text)


    def as_numeric(self):
        try:
            return int(self.type)

        except ValueError:
            return 0


    def __repr__(self):
        return repr(self.data)


    def __str__(self):
        return self.text


class Channel(object):
    """Class representing a channel on a IRC network
    """
    def __init__(self, name=False):
        self.name = name
        self.clients = []
        self.modes = False


    def __str__(self):
        return self.name


    def __cmp__(self, other):
        if self.name < other:
            return -1

        if self.name == other:
            return 0

        return 1



class Network(object):
    """Class representing a IRC network.
    """
    def __init__(self, client, name, host, port=6667, config=None, enabled=True):
        self.client = client
        self.config = config
        self.name = name
        self.enabled = enabled
        self.nick = config.get('nick')
        self.ident = config.get('ident')
        self.realname = config.get('realname')
        self.host = host
        self.port = port
        self.server = None
        self.users = {} # not implemented
        self.channels = {}
        self.lastping = (0, 0)
        self.green = None
        self.sock = None
        self.connection_state = OFFLINE
        self._buffer = ""
        self.timers = {}
        self.ircd_options = {}
        self.ircd_flags = []
        self.usermodes = set()

    def __str__(self):
        return self.name


    def __cmp__(self, other):
        if self.name < other:
            return -1

        if self.name == other:
            return 0

        return 1


    def init(self):
        """initializes the network, spawning a greenlet
        """
        self.green = gevent.spawn(self.run)


    def run(self):
        """Connects to the IRC network and performs the main loop.
        """
        logger.info("Starting run() %s", self.name)
        if self.enabled:
            self.connect()

        while True:
            if not self.enabled:
                gevent.sleep(1)
                continue

            if not self.connection_state:
                self.add_timer('connect', 30, self.connect, False)
                gevent.sleep(0.01)
                continue

            data = self.recv()
            if data is None:
                self.close()
                self.add_timer('connect', 30, self.connect)
                continue

            for line in data:
                self.parse(line)


    def kill_timer(self, timer):
        if timer in self.timers:
            self.timers[timer].kill()
            del self.timers[timer]


    def kill_all_timers(self):
        #horribe sytnax. to avoid RuntimeError: dictionary changed size during iteration
        for t in [x for x in self.timers.keys()]:
            self.timers[t].kill()
            del self.timers[t]


    def add_timer(self, timer, delay, callback, replace=True):
        if replace:
            self.kill_timer(timer)

        self.timers[timer] = gevent.spawn_later(delay, callback)


    def enable(self):
        #if self.enabled is False and 'connect' not in self.timers:
        #    self.add_timer('connect', 30, self.connect)
        self.enabled = True


    def disable(self):
        self.enabled = False
        if self.connection_state:
            self.disconnect()

        self.kill_all_timers()


    def recv(self):
        try:
            data = self.sock.recv(1024)
            data = data.decode()
            if data == '':
                logger.warning("No data on recv() for %s", self.name)
                return None

            left = not data.endswith("\n")
            data = self._buffer + data
            self._buffer = ""
            data = re.split('\r?\n', data)
            if left:
                self._buffer = data.pop()

            data = [line for line in data if len(line) > 0]
            return data

        except socket.error:
            logger.warning("Socket error on recv() for %s", self.name)
            return None

        except gevent.Timeout:
            logger.warning("Socket timeout on recv() for %s", self.name)
            return None


    def connect(self):
        """connects to the IRC network"""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.sock.connect((self.host, self.port))
            self.connection_state = CONNECTING
            self.send("NICK %s\r\nUSER %s 0 0: %s" % (self.nick, self.ident, self.realname))
            logger.info("Connecting to %s", self.name)
            self.kill_timer('connect')

        except socket.error:
            logger.info("Connection to %s failed. Retrying in 30...", self.name)
            self.connection_state = OFFLINE
            self.add_timer('connect', 30, self.connect)
            return False

        return True


    def close(self):
        if self.sock:
            self.sock.close()

        self.kill_all_timers()
        self.connection_state = OFFLINE


    def disconnect(self):
        """quits the network, closes the socket and kills the greenlet"""
        self.enabled = False
        self.send("QUIT")
        self.sock.shutdown(socket.SHUT_RDWR)
        self.close()
        #self.kill_all_timers() # redundant.


    def send(self, message, log=True):
        """sends a message over the socket"""
        if log:
            logger.debug("Send -> %s", repr(message))

        try:
            result = self.sock.sendall(bytearray(message + "\r\n", 'ascii'))
            if result == 0:
                logger.error("No Data on send() for %s", self.name)
                self.close()

        except socket.error:
            logger.error("Socket error on send() for %s", self.name)
            self.close()


    def ping(self):
        """pings the remote irc server"""
        self.send(f'PING {self.server}')


    def ctcp(self, dest, flag, message=None):
        """sends a PRIVMSG to the irc server"""
        if message:
            self.send(f'PRIVMSG {dest} :\001{flag} {message}\001')
        else:
            self.send(f'PRIVMSG {dest} :\001{flag}\001')


    def privmsg(self, dest, message):
        """sends a PRIVMSG to the irc server"""
        if not isinstance(message, (list, tuple)):
            message = [message]

        for msg in message:
            lines = re.split('[\r\n]+', str(msg))
            [self.send(f'PRIVMSG {dest} :{text}') for text in lines]


    def notice(self, dest, message):
        """sends a NOTICE to the irc server"""
        self.send(f'NOTICE {dest} :{message}')


    def join(self, dest):
        """sends a JOIN to the irc server"""
        self.send(f'JOIN {dest}')


    def part(self, dest, message):
        """sends a PART to the irc server"""
        self.send(f'PART {dest} :{message}')


    def getaccess(self, host):
        """Returns the access level for the specified host
        """
        for key, val in self.config.get('access', {}).items():
            if host == key:
                return val

        return 0


    def pingtimer(self):
        self.kill_timer('ping')
        if not self.connection_state or not self.enabled:
            return

        ctime = time.time()
        if ctime - self.lastping[0] > 60: # TODO: log reason
            self.close()
            return

        self.ping()
        self.add_timer('ping', 30, self.pingtimer)


    def parse(self, data):
        """parses the IRC message
        """
        event = Event(self, data)
        if event.type != 'PING':
            logger.debug("Recv <-- %s", repr(event))

        handler = getattr(self, '_event_%s' % event.type, None)
        if handler and callable(handler):
            handler(event) # pylint: disable=E1102

        self.client.handle_event(event)


    def _event_PING(self, event):
        ctime = time.time()
        self.lastping = (ctime, ctime - self.lastping[0])
        #logger.debug("ping time: %s seconds", self.lastping[1])
        self.send('PONG ' + event.text, False)


    def _event_PONG(self, event):
        ctime = time.time()
        self.lastping = (ctime, ctime - self.lastping[0])


    def _event_JOIN(self, event):
        if event.nick == self.nick: # we joined a channel
            self.channels[event.text] = Channel(event.text)


    def _event_PRIVMSG(self, event):
        # check ctcp
        match = RE_CTCP.match(event.text)
        if match:
            event.type = 'CTCP'
            event.ctcp = match.group(1)
            event.text = match.group(2)
            return

        if event.text[0] == self.config.get('command_prefix', ''):
            match = re.match(r"(\S+)(?:\s+(.+))?$", event.text[1:])
            if not match:
                return
            self.client.handle_command(match.group(1).lower(), match.group(2), event)


    def _event_NICK(self, event):
        if event.nick == self.nick:
            self.nick = event.dest
            if self.config.get('nickserv') and self.nick == self.config['nick']:
                self.privmsg('nickserv', 'identify %s' % self.config['nickserv'])


    def _event_MODE(self, event):
        if event.dest == self.nick:
            modes = re.findall(r'([\+\-])([^\+\-\s]+)', event.fulltext)
            if not modes:
                return

            for group in modes:
                call = None
                if group[0] == '+':
                    call = self.usermodes.add

                elif group[0] == '-':
                    call = self.usermodes.remove

                if not call:
                    continue

                for m in group[1]:
                    call(m)


    def _event_1(self, event):
        self.server = event.source


    def _event_4(self, event): # ircd version
        # not implmented yet.
        # need to detect ircd version and supported modes
        pass


    def _event_5(self, event): # ircd options
        text = re.sub(r' ?:.*$', '', event.fulltext)
        items = text.split()
        for setting in items:
            if '=' in setting:
                key, value = setting.split('=')
                try:
                    value = int(value)

                except ValueError:
                    pass

                self.ircd_options[key] = value

            else:
                self.ircd_flags.append(setting)


    def _event_376(self, event): # end of MOTD. fully connected
        self.connection_state = ONLINE
        for func in self.config.get('onconnect', []):
            try:
                func(self)

            except Exception as msg:
                logger.error("Exception thrown in onconnect callback: %s", msg)

        if self.config.get('nickserv') and self.nick == self.config['nick']:
            self.privmsg('nickserv', 'identify %s' % self.config['nickserv'])

        if "oper_id" in self.config and "oper_pass" in self.config:
            self.send("oper %s %s" % (self.config['oper_id'], self.config['oper_pass']))

        for chan in self.config.get('channels', []):
            self.join(chan)

        self.add_timer('ping', 30, self.pingtimer)


    def _event_381(self, event): # oper successful
        pass


    def _event_422(self, event): # no MOTD. fully connected
        self._event_376(event)


    def _event_433(self, event): # nickname taken
        # need to use a altnick if we get this while connecting. other times we dont care
        if self.connection_state == ONLINE:
            # checking if event.dest == '*' would work too (since we dont have a nick yet)
            return

        # our chosen nickname is taken.
        if self.nick == self.config['nick'] and 'altnick' in self.config:
            self.nick = self.config['altnick']

        else:
            self.nick = self.config['nick'] + str(random.randint(1, 1000))

        self.send("NICK %s" % self.nick)


    def _event_491(self, event): # invalid oper credentials
        pass


class Client(object):
    """Class represeting a IRC client.
    """
    def __init__(self):
        self.networks = {}
        self.config = None


    def add_network(self, name, host, port=6667, config=None, enabled=True):
        """Adds a IRC network. Note this does not initialize it.
        """
        if config is None:
            config = {}

        for key, value in self.config.items():
            if key not in ('plugins', 'networks') and not key.startswith("_"):
                config.setdefault(key, value)

        self.networks[name] = Network(
            self,
            name=name,
            host=host,
            port=port,
            config=config,
            enabled=enabled)

        return True


    def load(self, config):
        """Sets up the config file and adds all networks defined in it.
        """
        self.config = config
        config.setdefault('nick', 'irclite')
        config.setdefault('ident', 'irclite')
        config.setdefault('realname', 'irclite')
        for net in self.config.get('networks', []):
            self.add_network(
                name=net['name'],
                host=net['host'],
                port=net.get('port', 6667),
                config=net.get('config', None),
                enabled=net.get('enabled', True))


    def shutdown(self):
        """Disconnects and shuts down all Network objects
        """
        for network in self.networks.values():
            network.disconnect()
            network.green.kill()
            network.kill_all_timers()


    def init(self):
        """Performs any initialization actions and calls Network.init() for
        all Network objects
        """
        for network in self.networks.values():
            network.init()


    def run(self):
        """Waits for all Network greenlets to finish
        """
        gevent.joinall([x.green for x in self.networks.values()])
        logger.info("run() finished")


    def handle_command(self, command, args, event):
        pass


    def handle_event(self, event):
        pass
