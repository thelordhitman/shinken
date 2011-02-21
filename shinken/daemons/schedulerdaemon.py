#!/usr/bin/env python
#Copyright (C) 2009-2010 :
#    Gabes Jean, naparuba@gmail.com
#    Gerhard Lausser, Gerhard.Lausser@consol.de
#    Gregory Starck, g.starck@gmail.com
#    Hartmut Goebel, h.goebel@goebel-consult.de
#
#This file is part of Shinken.
#
#Shinken is free software: you can redistribute it and/or modify
#it under the terms of the GNU Affero General Public License as published by
#the Free Software Foundation, either version 3 of the License, or
#(at your option) any later version.
#
#Shinken is distributed in the hope that it will be useful,
#but WITHOUT ANY WARRANTY; without even the implied warranty of
#MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#GNU Affero General Public License for more details.
#
#You should have received a copy of the GNU Affero General Public License
#along with Shinken.  If not, see <http://www.gnu.org/licenses/>.

import os, sys, random, time


from shinken.scheduler import Scheduler
from shinken.objects import Config
from shinken.macroresolver import MacroResolver
from shinken.external_command import ExternalCommandManager
from shinken.daemon import Daemon
from shinken.util import to_int, to_bool
from shinken.modulesmanager import ModulesManager
import shinken.pyro_wrapper as pyro
from shinken.pyro_wrapper import Pyro
from shinken.log import logger

#Interface for Workers

class IChecks(Pyro.core.ObjBase):
    """ Interface for Workers:
They connect here and see if they are still OK with our running_id, if not, they must drop their checks """
    # in progress we keep sched link
    # and we create a running_id so poller and
    # reactionner know if we restart or not
    def __init__(self, sched):
        Pyro.core.ObjBase.__init__(self)
        self.sched = sched
        self.running_id = random.random()

    # poller or reactionner is asking us our running_id
    def get_running_id(self):
        return self.running_id


    # poller or reactionner ask us actions
    def get_checks(self , do_checks=False, do_actions=False, poller_tags=[]):
        #print "We ask us checks"
        res = self.sched.get_to_run_checks(do_checks, do_actions, poller_tags)
        #print "Sending %d checks" % len(res)
        self.sched.nb_checks_send += len(res)
        return res

    # poller or reactionner are putting us results
    def put_results(self, results):
        nb_received = len(results)
        self.sched.nb_check_received += nb_received
        print "Received %d results" % nb_received
        self.sched.waiting_results.extend(results)

        #for c in results:
        #self.sched.put_results(c)
        return True


class IBroks(Pyro.core.ObjBase):
    """ Interface for Brokers:
They connect here and get all broks (data for brokers). datas must be ORDERED! (initial status BEFORE uodate...) """ 
    # we keep sched link
    def __init__(self, sched):
        Pyro.core.ObjBase.__init__(self)
        self.sched = sched
        self.running_id = random.random()

    # Broker need to void it's broks?
    def get_running_id(self):
        return self.running_id

    # poller or reactionner ask us actions
    def get_broks(self):
        #print "We ask us broks"
        res = self.sched.get_broks()
        #print "Sending %d broks" % len(res)#, res
        self.sched.nb_broks_send += len(res)
        #we do not more have a full broks in queue
        self.sched.has_full_broks = False
        return res

    #A broker is a new one, if we do not have
    #a full broks, we clean our broks, and
    #fill it with all new values
    def fill_initial_broks(self):
        if not self.sched.has_full_broks:
            self.sched.broks.clear()
            self.sched.fill_initial_broks()

    #Ping? Pong!
    def ping(self):
        return None


class IForArbiter(Pyro.core.ObjBase):
    """ Interface for Arbiter, our big MASTER. We ask him a conf and after we listen for him.
HE got user entry, so we must listen him carefully and give information he want, maybe for another scheduler """
    def __init__(self, app):
        Pyro.core.ObjBase.__init__(self)
        self.app = app
        self.running_id = random.random()


    #very useful?
    def get_running_id(self):
        return self.running_id


    #use full too?
    def get_info(self, type, ref, prop, other):
        return self.app.sched.get_info(type, ref, prop, other)


    #arbiter is send us a external coomand.
    #it can send us global command, or specific ones
    def run_external_command(self, command):
        self.app.sched.run_external_command(command)


    #Arbiter is sending us a new conf. We check if we do not already have it.
    #If not, we take it, and if app has a scheduler, we ask it to die,
    #so the new conf  will be load, and a new scheduler created
    def put_conf(self, conf_package):
        self.use_ssl = self.app.use_ssl
        (conf, override_conf, modules, satellites) = conf_package
        if not self.app.have_conf or self.app.conf.magic_hash != conf.magic_hash:
            self.app.conf = conf
            self.app.override_conf = override_conf
            self.app.modules = modules
            self.app.satellites = satellites
            self.pollers = self.app.pollers
            print "Get conf:", self.app.conf
            self.app.have_conf = True
            print "Have conf?", self.app.have_conf
            print "Just apres reception"

            
            # Now We create our pollers
            for pol_id in satellites['pollers']:
                # Must look if we already have it
                already_got = pol_id in self.pollers
                p = satellites['pollers'][pol_id]
                self.pollers[pol_id] = p
                uri = pyro.create_uri(p['address'], p['port'], 'Schedulers', self.use_ssl)
                self.pollers[pol_id]['uri'] = uri
                self.pollers[pol_id]['last_connexion'] = 0
                print "Got a poller", p


            #if app already have a scheduler, we must say him to
            #DIE Mouahahah
            #So It will quit, and will load a new conf (and create a brand new scheduler)
            if self.app.sched:
                self.app.sched.die()


    #Arbiter want to know if we are alive
    def ping(self):
        return True


    #Use by arbiter to know if we have a conf or not
    #can be usefull if we must do nothing but
    #we are not because it can KILL US!
    def have_conf(self):
        return self.app.have_conf


    #Call by arbiter if it thinks we are running but we must do not (like
    #if I was a spare that take a conf but the master returns, I must die
    #and wait a new conf)
    #Us : No please...
    #Arbiter : I don't care, hasta la vista baby!
    #Us : ... <- Nothing! We are die! you don't follow
    #anything or what??
    def wait_new_conf(self):
        print "Arbiter want me to wait a new conf"
        self.app.have_conf = False
        if hasattr(self.app, "sched"):
            self.app.sched.die()


# The main app class
class Shinken(Daemon):

    properties = Daemon.properties.copy()
    properties.update({
        'pidfile':      { 'default' : '/usr/local/shinken/var/schedulerd.pid', 'pythonize' : None, 'path' : True},
        'port':         { 'default' : '7768', 'pythonize' : to_int},
        'local_log':    { 'default' : '/usr/local/shinken/var/schedulerd.log', 'pythonize' : None, 'path' : True},
    })
    
    
    #Create the shinken class:
    #Create a Pyro server (port = arvg 1)
    #then create the interface for arbiter
    #Then, it wait for a first configuration
    def __init__(self, config_file, is_daemon, do_replace, debug, debug_file):
        
        Daemon.__init__(self, 'scheduler', config_file, is_daemon, do_replace, debug, debug_file)

        self.sched = None
        self.ichecks = None
        self.ibroks = None
        self.must_run = True

        #Config Class must be filled with USERN Macro
        Config.fill_usern_macros()

        #Now the interface
        self.i_for_arbiter = IForArbiter(self)
        self.uri = None
        self.uri2 = None

        # And possible links for satellites
        # from now only pollers
        self.pollers = {}
        self.reactionners = {}


    def do_stop(self):
        #if hasattr(self, 'sched'):
        if self.sched:
            print "Asking for a retention save"
            self.sched.update_retention_file(forced=True)
        
        print "Stopping all network connexions"
        self.daemon.shutdown(True)

        # and closing the local log file if need
        logger.quit()


    # We wait (block) for arbiter to send us conf
    def wait_initial_conf(self):

        self.have_conf = False
        print "Waiting for initial configuration"
        timeout = 1.0
        while not self.have_conf and not self.interrupted:

            elapsed, _, _ = self.handleRequests(timeout)
            if elapsed:
                timeout -= elapsed
                if timeout > 0:
                    continue
            
            # Timeout
            sys.stdout.write(".")
            sys.stdout.flush()
            timeout = 1.0
        
        if self.interrupted:
            self.request_stop()



    #OK, we've got the conf, now we load it
    #and launch scheduler with it
    #we also create interface for poller and reactionner
    def load_conf(self):
        #First mix conf and override_conf to have our definitive conf
        for prop in self.override_conf:
            print "Overriding the property %s with value %s" % (prop, self.override_conf[prop])
            val = self.override_conf[prop]
            setattr(self.conf, prop, val)

        if self.conf.use_timezone != 'NOTSET':
            print "Setting our timezone to", self.conf.use_timezone
            os.environ['TZ'] = self.conf.use_timezone
            time.tzset()

        print "I've got modules", self.modules
        # TODO: if scheduler had previous modules instanciated it must clean them !
        self.modules_manager.set_modules(self.modules)
        self.do_load_modules()

        # create scheduler with ref of our daemon
        self.sched = Scheduler(self.daemon, self)

        # give it an interface
        # But first remove previous interface if exists
        if self.ichecks != None:
            print "Deconnecting previous Check Interface from daemon"
            self.daemon.unregister(self.ichecks)

        #Now create and connect it
        self.ichecks = IChecks(self.sched)
        self.uri = self.daemon.register(self.ichecks, "Checks")
        print "The Checks Interface uri is:", self.uri

        #Same for Broks
        if self.ibroks != None:
            print "Deconnecting previous Broks Interface from daemon"
            self.daemon.unregister(self.ibroks)

        #Create and connect it
        self.ibroks = IBroks(self.sched)
        self.uri2 = self.daemon.register(self.ibroks, "Broks")
        print "The Broks Interface uri is:", self.uri2

        print "Loading configuration"
        self.conf.explode_global_conf()
        #we give sched it's conf
        self.sched.load_conf(self.conf)

        self.sched.load_satellites(self.pollers, self.reactionners)

        #We must update our Config dict macro with good value
        #from the config parameters
        self.sched.conf.fill_resource_macros_names_macros()
        print "DBG: got macors", self.sched.conf.macros
        print getattr(self.sched.conf, '$PLUGINSDIR$', 'MONCUL'*100)

        #Creating the Macroresolver Class & unique instance
        m = MacroResolver()
        m.init(self.conf)

        #self.conf.dump()
        #self.conf.quick_debug()

        #Now create the external commander
        #it's a applyer : it role is not to dispatch commands,
        #but to apply them
        e = ExternalCommandManager(self.conf, 'applyer')

        #Scheduler need to know about external command to
        #activate it if necessery
        self.sched.load_external_command(e)

        #External command need the sched because he can raise checks
        e.load_scheduler(self.sched)


    def compensate_system_time_change(self, difference):
        """ Compensate a system time change of difference for all hosts/services/checks/notifs """
        logger.log('Warning: A system time change of %d has been detected.  Compensating...' % difference)
        # We only need to change some value
        self.program_start = max(0, self.program_start + difference)

        # Then we compasate all host/services
        for h in self.sched.hosts:
            h.compensate_system_time_change(difference)
        for s in self.sched.services:
            s.compensate_system_time_change(difference)

        # Now all checks and actions
        for c in self.sched.checks.values():
            # Already launch checks should not be touch
            if c.status == 'scheduled':
                t_to_go = c.t_to_go
                ref = c.ref
                new_t = max(0, t_to_go + difference)
                # But it's no so simple, we must match the timeperiod
                new_t = ref.check_period.get_next_valid_time_from_t(new_t)
                # But maybe no there is no more new value! Not good :(
                # Say as error, with error output
                if new_t == None:
                    c.state = 'waitconsume'
                    c.exit_status = 2
                    c.output = '(Error: there is no available check time after time change!)'
                    c.check_time = time.time()
                    c.execution_time = 0
                else:
                    c.t_to_go = new_t
                    ref.next_chk = new_t

        # Now all checks and actions
        for c in self.sched.actions.values():
            # Already launch checks should not be touch
            if c.status == 'scheduled':
                t_to_go = c.t_to_go

                #  Event handler do not have ref
                ref = getattr(c, 'ref', None)
                new_t = max(0, t_to_go + difference)

                # Notification should be check with notification_period
                if c.is_a == 'notification':
                    # But it's no so simple, we must match the timeperiod
                    new_t = ref.notification_period.get_next_valid_time_from_t(new_t)
                    # And got a creation_time variable too
                    c.creation_time = c.creation_time + difference

                # But maybe no there is no more new value! Not good :(
                # Say as error, with error output
                if new_t == None:
                    c.state = 'waitconsume'
                    c.exit_status = 2
                    c.output = '(Error: there is no available check time after time change!)'
                    c.check_time = time.time()
                    c.execution_time = 0
                else:
                    c.t_to_go = new_t



    def manage_signal(self, sig, frame):
        # self.sched could be still unset if we have not yet received our conf
        if self.sched: 
            self.sched.must_run = False
        self.must_run = False
        Daemon.manage_signal(self, sig, frame)


    def do_loop_turn(self):
        self.sched.run()
        # Ok, we quit scheduler, but maybe it's just for
        # reloading our configuration
        if self.must_run:
            if self.have_conf:
                self.load_conf()
            else:
                self.wait_initial_conf()
                self.load_conf()


    #our main function, launch after the init
    def main(self):

        self.do_load_config()
        
        self.do_daemon_init_and_start()
        
        self.uri2 = self.daemon.register(self.i_for_arbiter, "ForArbiter")
        print "The Arbiter Interface is at:", self.uri2

        # Ok, now the conf
        self.wait_initial_conf()
        print "Ok we've got conf"

        # ok, if we are here, we've got the conf
        self.load_conf()

        print "Configuration Loaded"
        
        self.do_mainloop()
            
