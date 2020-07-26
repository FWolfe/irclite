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
import gevent
import logging

RE_TYPE = re.compile(r"^(?:\:(\S+)|\:?([a-z0-9A-Z_-]+\.[a-z0-9A-Z_\.-]+))$")
RE_SENDER = re.compile(r"^\:?([^\!]+)\!([^\@]+)\@(\S+)$")

logger = logging.getLogger(__name__)
class Message(object):
    """Class representing a IRC message
    """
    __slots__ = ('network', 'text', 'dest', 'type', 'sender',
                 'nick', 'ident', 'host', 'chan', 'tokens', 'raw')

    def __init__(self, network, raw):
        self.text = None
        self.dest = None
        self.type = None
        self.sender = None
        self.nick = None
        self.ident = None
        self.host = None
        self.chan = None

        self.network = network
        self.raw = raw
        self.tokens = raw.split()

        match = RE_TYPE.match(self.tokens[0])
        if match:
            self.sender = match.group(1) or match.group(2)
            self.type = self.tokens[1]
        else:
            self.type = self.tokens[0]

        match = re.search(r"\:(.*)$", raw[1:])
        if match:
            self.text = match.group(1)

        if len(self.tokens) > 2 and self.type == self.tokens[1]:
            self.dest = self.tokens[2]
            if self.dest.startswith('#'):
                self.chan = self.dest

        if self.sender:
            match = RE_SENDER.match(self.sender)
            if match:
                self.nick = match.group(1)
                self.ident = match.group(2)
                self.host = match.group(3)


    def reply(self, text):
        if self.type != 'PRIVMSG':
            return # TODO: raise exception
        if self.chan:
            self.network.privmsg(self.chan, text)
        else:
            self.network.privmsg(self.sender, text)


    def is_numeric(self):
        try:
            int(self.type)
            return True
        except ValueError:
            return False


    def as_command(self, start=1, stop=None):
        """
        returns the text as if it was a irc bot's command arguments
        ie:
            a text of ".weather mycity, mycountry"
            returns "mycity, mycountry"
        """
        try:
            return ' '.join(self.text.split()[start:stop])
        except IndexError:
            return None


    def __repr__(self):
        return self.raw

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
        elif self.name == other:
            return 0
        else:
            return -1



class Network(object):
    """Class representing a IRC network.
    """
    def __init__(self, parent, name, host, port, config, enabled=True):
        self._parent = parent
        self.config = config
        self.name = name
        self.enabled = enabled
        self.nick = config.get('nick')
        self.ident = config.get('ident')
        self.realname = config.get('realname')
        self.host = host
        self.port = port
        self.server = None
        self.users = {}
        self.channels = {}
        self.lastping = (0, 0)
        self.green = None
        self.sock = None
        self.connected = False
        self._buffer = ""
        self._ctimer = None
        self._ptimer = None

    def __str__(self):
        return self.name

    def __cmp__(self, other):
        if self.name < other:
            return -1
        elif self.name == other:
            return 0
        else:
            return -1

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

            if not self.connected:
                if not self._ctimer:
                    self._ctimer = gevent.spawn_later(30, self.connect)
                gevent.sleep(0.01)
                continue

            data = self.recv()
            if data is None:
                self.close()
                self._ctimer = gevent.spawn_later(30, self.connect)
                continue

            for line in data:
                self.parse(Message(self, line))

    def _kill_timers(self):
        if self._ctimer:
            self._ctimer.kill()
        if self._ptimer:
            self._ptimer.kill()
        self._ctimer = None
        self._ptimer = None

    def enable(self):
        if self.enabled is False:
            if not self._ctimer:
                self._ctimer = gevent.spawn_later(30, self.connect)
        self.enabled = True

    def disable(self):
        self.enabled = False
        if self.connected:
            self.disconnect()
        self._kill_timers()

    def recv(self):
        try:
            data = self.sock.recv(1024)
            data = data.decode()
            if data == '':
                logger.warning("No data on recv() for %s", self.name)
                return None # socket broken

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
            self.connected = True
            self.send("NICK %s\r\nUSER %s 0 0: %s" % (self.nick, self.ident, self.realname))
            logger.info("Connecting to %s", self.name)
            self._ctimer = None
        except socket.error:
            logger.info("Connection to %s failed. Retrying in 30..." % self.name)
            self.connected = False
            self._ctimer = gevent.spawn_later(30, self.connect)
            return False
        return True


    def close(self):
        if self.sock:
            self.sock.close()
        self._kill_timers()
        self.connected = False


    def disconnect(self):
        """quits the network, closes the socket and kills the greenlet"""
        self.enabled = False
        self.send("QUIT")
        self.sock.shutdown(socket.SHUT_RDWR)
        self.close()
        self._kill_timers()

    def send(self, msg):
        """sends a message over the socket"""
        logger.debug("Send -> %s", msg)
        try:
            result = self.sock.sendall(bytearray(msg + "\r\n", 'ascii'))
            if result == 0:
                print("Error: %s - No Data on send" % self.name)
                self.close()
        except socket.error:
            print("Error: %s - Socket error on recv" % self.name)
            self.close()

    def ping(self):
        """pings the remote irc server"""
        self.send("PING %s" % self.server)

    def privmsg(self, dest, message):
        """sends a PRIVMSG to the irc server"""
        if not isinstance(message, list) and not isinstance(message, tuple):
            message = [message]
        for msg in message:
            lines = re.split('[\r\n]+', str(msg))
            [self.send("PRIVMSG %s :%s" % (dest, x)) for x in lines]


    def notice(self, dest, msg):
        """sends a NOTICE to the irc server"""
        self.send("NOTICE %s :%s" %(dest, msg))

    def join(self, dest):
        """sends a JOIN to the irc server"""
        self.send("JOIN %s" % dest)


    def part(self, dest, msg):
        """sends a PART to the irc server"""
        self.send("PART %s :%s" %(dest, msg))


    def getaccess(self, host):
        """Returns the access level for the specified host
        """
        for key, val in self.config.get('access', {}).items():
            if host == key:
                return val
        return 0


    def pingtimer(self):
        self._ptimer = None
        if not self.connected or not self.enabled:
            return
        ctime = time.time()
        if ctime - self.lastping[0] > 60:
            self.close()
            return
        self.ping()
        self._ptimer = gevent.spawn_later(30, self.pingtimer)


    def parse(self, msg):
        """parses the IRC message
        """
        config = self.config
        logger.debug("Recv <-- %s", repr(msg))

        if msg.type == 'PING':
            ctime = time.time()
            self.lastping = (ctime, ctime - self.lastping[0])
            logger.debug("ping time: %s seconds", self.lastping[1])
            self.send('PONG ' + msg.text)

        elif msg.type == 'PONG':
            ctime = time.time()
            self.lastping = (ctime, ctime - self.lastping[0])

        elif msg.type == '1' or msg.type == '001':
            self.server = msg.sender

        elif (msg.type == '376' or msg.type == '422'):
            for func in config.get('onconnect', []):
                try:
                    func(msg.network)
                except:
                    pass
            for chan in config.get('channels', []):
                self.join(chan)

            self._ptimer = gevent.spawn_later(30, self.pingtimer)

        elif msg.type == 'NICK':
            pass

        elif msg.type == 'JOIN':
            if msg.nick == self.nick: # we joined a channel
                self.channels[msg.text] = Channel(msg.text)

        elif msg.type == 'PART':
            if msg.nick == config.nick:
                del self.channels[msg.text]

        self._parent.trigger_event(self, msg)

        if msg.type == 'PRIVMSG' and msg.text[0] == self.config.get('control', '.'):
            tokens = msg.text[1:].split(' ')
            cmd = tokens[0].lower()
            self._parent.trigger_command(self, msg, cmd)



class Client(object):
    """Class represeting a IRC client.
    """
    def __init__(self):
        self.active = None
        self.networks = {}
        self.config = None


    def add_network(self, name, host, port=6667, config=False, enabled=True):
        """Adds a IRC network. Note this does not initialize it.
        """
        if not config:
            config = self.config
        self.networks[name] = Network(self, name, host, port, config, enabled)
        return True


    def load(self, config):
        """Adds sets up the config file and adds all networks defined in it.
        """
        self.config = config
        for net in self.config.get('networks', []):
            self.add_network(
                net['name'],
                net['host'],
                net.get('port', 6667),
                net.get('config', self.config),
                net.get('enabled', True))


    def shutdown(self):
        """Disconnects and shuts down all Network objects
        """
        for key, value in self.networks.items():
            value.disconnect()
            value.green.kill()
            value._kill_timers()


    def init(self):
        """Performs any initialization actions and calls Network.init() for
        all Network objects
        """
        for key, value in self.networks.items():
            value.init()


    def run(self):
        """Waits for all Network greenlets to finish
        """
        gevent.joinall([x.green for x in self.networks.values()])
        logger.info("run() finished")


    def trigger_command(self, network, msg, cmd):
        pass

    def trigger_event(self, network, msg):
        pass

