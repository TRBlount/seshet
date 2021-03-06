"""Implement SeshetBot as subclass of ircutils3.bot.SimpleBot."""

import logging
import os
from io import StringIO
from datetime import datetime

from ircutils3 import bot, client

from .utils import KVStore, Storage, IRCstr


class SeshetUser(object):
    """Represent one IRC user."""
    
    def __init__(self, nick, user, host):
        logging.debug("Building new SeshetUser, %s", nick)
        self.nick = IRCstr(nick)
        self.user = user
        self.host = host
        self.channels = []
        
    def join(self, channel):
        """Add this user to the channel's user list and add the channel to this
        user's list of joined channels.
        """
        
        if channel not in self.channels:
            channel.users.add(self.nick)
            self.channels.append(channel)
    
    def part(self, channel):
        """Remove this user from the channel's user list and remove the channel
        from this user's list of joined channels.
        """
        
        if channel in self.channels:
            channel.users.remove(self.nick)
            self.channels.remove(channel)
            
    def quit(self):
        """Remove this user from all channels and reinitialize the user's list
        of joined channels.
        """
        
        for c in self.channels:
            c.users.remove(self.nick)
        self.channels = []
        
    def change_nick(self, nick):
        """Update this user's nick in all joined channels."""
        
        old_nick = self.nick
        self.nick = IRCstr(nick)
        
        for c in self.channels:
            c.users.remove(old_nick)
            c.users.add(self.nick)
            
    def __str__(self):
        return "{}!{}@{}".format(self.nick, self.user, self.host)
        
    def __repr__(self):
        temp = "<SeshetUser {}!{}@{} in channels {}>"
        return temp.format(self.nick, self.user, self.host, self.channels)
        
        
class SeshetChannel(object):
    """Represent one IRC channel."""
    
    def __init__(self, name, users, log_size=100):
        self.name = IRCstr(name)
        self.users = users
        self.message_log = []
        self._log_size = log_size
        
    def log_message(self, user, message):
        """Log a channel message.
        
        This log acts as a sort of cache so that recent activity can be searched
        by the bot and command modules without querying the database.
        """
        
        if isinstance(user, SeshetUser):
            user = user.nick
        elif not isinstance(user, IRCstr):
            user = IRCstr(user)

        time = datetime.utcnow()
            
        self.message_log.append((time, user, message))
        
        while len(self.message_log) > self._log_size:
            del self.message_log[0]
        
    def __str__(self):
        return str(self.name)
        
    def __repr__(self):
        temp = "<SeshetChannel {} with {} users>"
        return temp.format(self.name, len(self.users))


class SeshetBot(bot.SimpleBot):
    """Extend `ircutils3.bot.SimpleBot`.
    
    Each instance represents one bot, connected to one IRC network.
    Each instance should have its own database, but can make use of
    any shared command modules. The modules may have to be added to
    the bot's database if the bot wasn't created using the
    `seshet --config` or `seshet --new` commands.
    """
    
    def __init__(self, nick='Seshet', db=None, debug_file=None, verbosity=99):
        """Extend `ircutils3.bot.SimpleBot.__init__()`.
        
        Keyword argument `db` is required for running commands other
        than core commands and should be an instance of pydal.DAL.
        """
        
        # initialize debug logging
        if debug_file is None:
            logging.basicConfig(level=verbosity)
        else:
            logging.basicConfig(filename=os.path.expanduser(debug_file),
                                level=verbosity
                                )
        
        logging.debug("Running `SimpleBot.__init__`...")
        bot.SimpleBot.__init__(self, nick, auto_handle=False)
        
        # define defaults
        
        self.session = Storage()
        
        self.log_file = 'seshet.log'
        self.log_formats = {}
        self.locale = {}
        
        self.channels = {}
        self.users = {}
        
        if db is None:
            # no database connection, only log to file and run
            # core command modules
            logging.info("No db, IRC logging will be done to file")
            self.log = self._log_to_file
            self.run_modules = self._run_only_core
            
            # dummy KV store since no db
            self.storage = Storage()
        else:
            logging.info("Using database %s", db)
            self.db = db
            self.storage = KVStore(db)
        
        # Add default handlers
        logging.debug("Adding default handlers...")
        self.events["any"].add_handler(client._update_client_info)
        self.events["ctcp_version"].add_handler(client._reply_to_ctcp_version)
        self.events["name_reply"].add_handler(_add_channel_names)
        
    def log(self, etype, source, msg='', target='', hostmask='', params=''):
        """Log an event in the database.
        
        Required:
            `etype` - event type. One of 'PRIVMSG', 'QUIT', 'PART', 'ACTION',
                'NICK', 'JOIN', 'MODE', 'KICK', 'CTCP', or 'ERROR'. Enforced 
                by database model.
            `source` - source of the event. Usually a user. For NICK events,
                the user's original nickname. For ERROR events, this should be
                the exception name, the module name, and the line number. The
                full traceback will be logged in `msg`.
        Optional:
            `msg` - a message associated with the event.
            `target` - the target the message was directed to. For MODE and KICK
                events, this will be the user the event was performed on. For
                NICK events, this will be channel the event was seen in (an event
                will be created for each channel the user is seen by the bot in).
            `hostmask` - a hostmask associated with the event.
            `parms` - any additional parameters associated with the event, such as
                a new nickname (for NICK events), mode switches (for MODE events),
                or a dump of local variables (for ERROR events).
        """
        
        self.db.event_log.insert(event_type=etype,
                                 event_time=datetime.utcnow(),
                                 source=source,
                                 target=target,
                                 message=msg,
                                 host=hostmask,
                                 params=params,
                                 )
        self.db.commit()
        
    def run_modules(self, e):
        # grab local pointer to self.db for faster lookup
        db = self.db
        
        # get initial list of modules handling this event type
        event_types = db.modules.event_types
        mod_enabled = db.modules.enabled
        init_mods = db(event_types.contains(e.command) & mod_enabled).select()
        
        logging.debug(("Running modules for {} command. "
                       "Initial module list:\n{}").format(e.command, init_mods)
                      )
        
        if e.command in ('PRIVMSG', 'CTCP_ACTION', 'NOTICE'):
            # narrow down list of modules to run based on event parameters
            
            # lowercase for non-caps comparisons
            m_low = e.message.lower()
            bot_n = self.nickname.lower()
            bot_u = self.user.lower()
            bot_r = self.real_name.lower()
            
            # indicates whether or not name has already been stripped from
            # original message
            for_us = False
            
            if e.target.startswith('#'):
                chan_msg = True
                chan_nicks = self.channels[e.target].users
            else:
                chan_msg = False
            
            fin_mods = list()  # final list of modules to run
            for mod in init_mods:
                if e.source in mod.whitelist:
                    fin_mods.append(mod)
                
                elif e.source in mod.blacklist:
                    pass
                
                if self.nickname in mod.enicks:                    
                    if e.target == self.nickname or for_us:
                        fin_mods.append(mod)
                    elif m_low.startswith(bot_n):
                        # strip nickname from original message so modules can
                        # process it correctly
                        e.message = e.message[len(bot_n):].lstrip(',: ')
                        
                        fin_mods.append(mod)
                        for_us = True
                    elif m_low.startswith(bot_u):
                        e.message = e.message[len(bot_u):].lstrip(',: ')
                        
                        fin_mods.append(mod)
                        for_us = True
                    elif m_low.startswith(bot_r):
                        e.message = e.message[len(bot_r):].lstrip(',: ')
                        
                        fin_mods.append(mod)
                        for_us = True
                
                if chan_msg:
                    if e.target in mod.dchannels:
                        pass
                    elif set(mod.dnicks) & chan_nicks:
                        pass
                    elif e.target in mod.echannels:
                        fin_mods.append(mod)
                    elif set(mod.enicks) & chan_nicks:
                        fin_mods.append(mod)
                        
            argv = m_low.split()
            for mod in fin_mods:
                # run each module
                m = __import__(mod.name)  # TODO: use importlib
                
                # TODO: add authentication and rate limiting
                
                for cmd, fun in m.commands.items():
                    if (mod.cmd_prefix + cmd) == argv[0]:
                        fun(self, e)
                        break
    
    def get_unique_users(self, chan):
        """Get the set of users that are unique to the given channel (i.e. not
        present in any other channel the bot is in).
        """
        
        chan = IRCstr(chan)
        
        these_users = self.channels[chan].users
        other_users = set()
        for c in self.channels.values():
            if c.name != chan:
                other_users |= c.users
        
        return these_users - other_users
    
    def on_message(self, e):
        self.log('privmsg',
                 source=e.source,
                 msg=e.message,
                 target=e.target,
                 )
        if e.target in self.channels:
            # TODO: move this to self.log() so we don't have to get time twice?
            self.channels[e.target].log_message(e.source, e.message)
        self.run_modules(e)
    
    def on_join(self, e):
        self.log('join',
                 source=e.source,
                 target=e.target,
                 hostmask=e.user+'@'+e.host,
                 )
                 
        chan = IRCstr(e.target)
        nick = IRCstr(e.source)
        if e.source != self.nickname:
            if nick not in self.users:
                self.users[nick] = SeshetUser(nick, e.user, e.host)
            self.users[nick].join(self.channels[chan])
                 
        self.run_modules(e)
    
    def on_part(self, e):
        self.log('part',
                 source=e.source,
                 hostmask=e.user+'@'+e.host,
                 msg=' '.join(e.params[1:]),
                 target=e.target,
                 )
        
        chan = IRCstr(e.target)
        nick = IRCstr(e.source)
        channel = self.channels[chan]
        user = self.users[nick]
        
        user.part(channel)
        if nick == self.nickname:
            # bot parted, remove that channel from all users and
            # remove any users with empty channel lists
            for u in self.users.values():
                if channel in u.channels:
                    u.channels.remove(channel)
                if len(u.channels) == 0:
                    del self.users[u.nick]
    
    def on_quit(self, e):
        nick = IRCstr(e.source)
        
        for chan in self.channels.values():
            if nick in chan.users:
                self.log('quit',
                         source=e.source,
                         hostmask=e.user+'@'+e.host,
                         msg=' '.join(e.params),
                         target=chan.name,
                         )
                         
        self.users[nick].quit()
        del self.users[nick]
    
    def on_disconnect(self, e):
        pass
    
    def on_kick(self, e):
        self.log('kick',
                 source=e.source,
                 target=e.target,
                 params=e.params[0],
                 msg=' '.join(e.params[1:]),
                 hostmask=e.user+'@'+e.host,
                 )
        
        chan = IRCstr(e.target)
        nick = IRCstr(e.source)
        channel = self.channels[chan]
        user = self.users[nick]
        
        user.part(channel)
        if nick == self.nickname:
            # bot parted, remove that channel from all users and
            # remove any users with empty channel lists
            for u in self.users.values():
                if channel in u.channels:
                    u.channels.remove(channel)
                if len(u.channels) == 0:
                    del self.users[u.nick]
    
    def on_nick_change(self, e):
        new_nick = IRCstr(e.target)
        old_nick = IRCstr(e.source)
        
        for chan in self.channels.values():
            if e.source in chan.user_list:
                self.log('nick',
                         source=e.source,
                         hostmask=e.user+'@'+e.host,
                         target=chan.name,
                         params=e.target,
                         )
        
        self.users[old_nick].change_nick(new_nick)
        self.users[new_nick] = self.users[old_nick]
        del self.users[old_nick]
    
    def on_ctcp_action(self, e):
        self.log('action',
                 source=e.source,
                 target=e.target,
                 msg=' '.join(e.params),
                 )
    
    def on_welcome(self, e):
        pass
    
    def on_mode(self, e):
        self.log('mode',
                 source=e.source,
                 msg=' '.join(e.params),
                 target=e.target,
                 )
    
    def before_poll(self):
        """Called each loop before polling sockets for I/O."""
        pass
    
    def after_poll(self):
        """Called each loop after polling sockets for I/O and
        handling any queued events.
        """
        pass
    
    def connect(self, *args, **kwargs):
        """Extend `client.SimpleClient.connect()` with defaults"""
        defaults = {}

        for i, k in enumerate(('host', 'port', 'channel', 'use_ssl', 'password')):
            if i < len(args):
                defaults[k] = args[i]
            elif k in kwargs:
                defaults[k] = kwargs[k]
            else:
                def_k = 'default_' + k
                defaults[k] = getattr(self, def_k, None)

        if defaults['use_ssl'] is None:
            defaults['use_ssl'] = False

        if defaults['host'] is None:
            raise TypeError("missing 1 required positional argument: 'host'")

        logging.info("Connecting to %s:%s and joining channels %s",
                     defaults['host'],
                     defaults['port'],
                     defaults['channel'],
                     )
        client.SimpleClient.connect(self, **defaults)

    def start(self):
        logging.debug("Beginning poll loop")
        self._loop(self.conn._map)
    
    def _log_to_file(self, etype, source, msg='', target='', hostmask='', params=''):
        """Override `log()` if bot is not initialized with a database
        connection. Do not call this method directly.
        """
        today = datetime.utcnow()
        # TODO: Use self.locale['timezone'] for changing time
        date = today.strftime(self.locale['date_fmt'])
        time = today.strftime(self.locale['time_fmt'])
        datetime_s = today.strftime(self.locale['short_datetime_fmt'])
        datetime_l = today.strftime(self.locale['long_datetime_fmt'])
        
        if target == self.nickname and etype in ('privmsg', 'action'):
            target = source

        if etype in self.log_formats:
            file_path = os.path.expanduser(self.log_file.format(**locals()))
            file_dir = os.path.dirname(file_path)
            if not os.path.isdir(file_dir):
                os.makedirs(file_dir)

            line = self.log_formats[etype].format(**locals())
            with open(file_path, 'a') as log:
                log.write(line+'\n')
        # else do nothing
    
    def _run_only_core(self, *args, **kwargs):
        """Override `_run_commands()` if bot is not initialized with a
        database connection. Do not call this method directly.
        
        Rather than getting a list of enabled modules from the database,
        Seshet will only run the commands defined by `core` in this package.
        The bot will only run commands given in private message ("query")
        by either an authenticated user defined in the instance's config file,
        or by any user with the same hostmask if authentication isn't set up
        in the instance's config file.
        
        The `core` command module from this package can only be overridden if
        the bot is initialized with a database connection and a new `core`
        module is entered into the database.
        """
        pass

    def _loop(self, map):
        """The main loop. Poll sockets for I/O and run any other functions
        that need to be run every loop.
        """
        try:
            from asyncore import poll
        except ImportError:
            raise Exception("Couldn't find poll function. Cannot start bot.")
    
        while map:
            self.before_poll()
            poll(timeout=30.0, map=map)
            self.after_poll()


def _add_channel_names(client, e):
        """Add a new channel to self.channels and initialize its user list.

        Called as event handler for RPL_NAMES events. Do not call directly.
        """

        chan = IRCstr(e.channel)
        names = set([IRCstr(n) for n in e.name_list])
        client.channels[chan] = SeshetChannel(chan, names)