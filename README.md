# irclite

a minimal lightwight IRC client API using gevent

```python
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
```
