# -*- coding: utf-8 -*-
'''
Zeromq transport classes
'''

import os
import errno
import hashlib

from random import randint

# Import Salt Libs
import salt.auth
import salt.crypt
import salt.utils
import salt.utils.verify
import salt.utils.event
import salt.payload
import logging


import salt.transport.client
import salt.transport.server
import salt.transport.mixins.auth
from salt.exceptions import SaltReqTimeoutError

import zmq
import zmq.eventloop.ioloop
# support pyzmq 13.0.x, TODO: remove once we force people to 14.0.x
if not hasattr(zmq.eventloop.ioloop, 'ZMQIOLoop'):
    zmq.eventloop.ioloop.ZMQIOLoop = zmq.eventloop.ioloop.IOLoop
import zmq.eventloop.zmqstream

# tornado imports
import tornado
import tornado.gen
import tornado.concurrent

log = logging.getLogger(__name__)


class AsyncZeroMQReqChannel(salt.transport.client.ReqChannel):
    '''
    Encapsulate sending routines to ZeroMQ.

    ZMQ Channels default to 'crypt=aes'
    '''

    def __init__(self, opts, **kwargs):
        self.opts = dict(opts)
        self.ttype = 'zeromq'

        # crypt defaults to 'aes'
        self.crypt = kwargs.get('crypt', 'aes')

        if 'master_uri' in kwargs:
            self.opts['master_uri'] = kwargs['master_uri']

        self._io_loop = kwargs.get('io_loop') or tornado.ioloop.IOLoop.current()

        if self.crypt != 'clear':
            # we don't need to worry about auth as a kwarg, since its a singleton
            self.auth = salt.crypt.AsyncAuth(self.opts, io_loop=self._io_loop)
        self.message_client = AsyncReqMessageClient(self.opts,
                                                    self.master_uri,
                                                    io_loop=self._io_loop,
                                                    )

    def __del__(self):
        '''
        Since the message_client creates sockets and assigns them to the IOLoop we have to
        specifically destroy them, since we aren't the only ones with references to the FDs
        '''
        self.message_client.destroy()

    @property
    def master_uri(self):
        return self.opts['master_uri']

    def _package_load(self, load):
        return {
            'enc': self.crypt,
            'load': load,
        }

    @tornado.gen.coroutine
    def crypted_transfer_decode_dictentry(self, load, dictkey=None, tries=3, timeout=60):
        if not self.auth.authenticated:
            yield self.auth.authenticate()
        ret = yield self.message_client.send(self._package_load(self.auth.crypticle.dumps(load)), timeout=timeout)
        key = self.auth.get_keys()
        aes = key.private_decrypt(ret['key'], 4)
        pcrypt = salt.crypt.Crypticle(self.opts, aes)
        raise tornado.gen.Return(pcrypt.loads(ret[dictkey]))

    @tornado.gen.coroutine
    def _crypted_transfer(self, load, tries=3, timeout=60):
        '''
        In case of authentication errors, try to renegotiate authentication
        and retry the method.
        Indeed, we can fail too early in case of a master restart during a
        minion state execution call
        '''
        @tornado.gen.coroutine
        def _do_transfer():
            data = yield self.message_client.send(self._package_load(self.auth.crypticle.dumps(load)),
                                      timeout=timeout,
                                      )
            # we may not have always data
            # as for example for saltcall ret submission, this is a blind
            # communication, we do not subscribe to return events, we just
            # upload the results to the master
            if data:
                data = self.auth.crypticle.loads(data)
            raise tornado.gen.Return(data)
        if not self.auth.authenticated:
            yield self.auth.authenticate()
        try:
            ret = yield _do_transfer()
        except salt.crypt.AuthenticationError:
            yield self.auth.authenticate()
            ret = yield _do_transfer()
        raise tornado.gen.Return(ret)

    @tornado.gen.coroutine
    def _uncrypted_transfer(self, load, tries=3, timeout=60):
        ret = yield self.message_client.send(self._package_load(load), timeout=timeout)
        raise tornado.gen.Return(ret)

    @tornado.gen.coroutine
    def send(self, load, tries=3, timeout=60):
        '''
        Send a request, return a future which will complete when we send the message
        '''
        if self.crypt == 'clear':
            ret = yield self._uncrypted_transfer(load, tries=tries, timeout=timeout)
        else:
            ret = yield self._crypted_transfer(load, tries=tries, timeout=timeout)
        raise tornado.gen.Return(ret)


class AsyncZeroMQPubChannel(salt.transport.mixins.auth.AESPubClientMixin, salt.transport.client.AsyncPubChannel):
    def __init__(self,
                 opts,
                 **kwargs):
        self.opts = opts
        self.ttype = 'zeromq'

        if 'io_loop' in kwargs:
            self.io_loop = kwargs['io_loop']
        else:
            self.io_loop = tornado.ioloop.IOLoop()

        self.hexid = hashlib.sha1(self.opts['id']).hexdigest()

        self.auth = salt.crypt.AsyncAuth(self.opts, io_loop=self.io_loop)

        self.serial = salt.payload.Serial(self.opts)

        self.context = zmq.Context()
        self._socket = self.context.socket(zmq.SUB)

        if self.opts['zmq_filtering']:
            # TODO: constants file for "broadcast"
            self._socket.setsockopt(zmq.SUBSCRIBE, 'broadcast')
            self._socket.setsockopt(zmq.SUBSCRIBE, self.hexid)
        else:
            self._socket.setsockopt(zmq.SUBSCRIBE, '')

        self._socket.setsockopt(zmq.SUBSCRIBE, '')
        self._socket.setsockopt(zmq.IDENTITY, self.opts['id'])

        # TODO: cleanup all the socket opts stuff
        if hasattr(zmq, 'TCP_KEEPALIVE'):
            self._socket.setsockopt(
                zmq.TCP_KEEPALIVE, self.opts['tcp_keepalive']
            )
            self._socket.setsockopt(
                zmq.TCP_KEEPALIVE_IDLE, self.opts['tcp_keepalive_idle']
            )
            self._socket.setsockopt(
                zmq.TCP_KEEPALIVE_CNT, self.opts['tcp_keepalive_cnt']
            )
            self._socket.setsockopt(
                zmq.TCP_KEEPALIVE_INTVL, self.opts['tcp_keepalive_intvl']
            )

        recon_delay = self.opts['recon_default']

        if self.opts['recon_randomize']:
            recon_delay = randint(self.opts['recon_default'],
                                  self.opts['recon_default'] + self.opts['recon_max']
                          )

            log.debug("Generated random reconnect delay between '{0}ms' and '{1}ms' ({2})".format(
                self.opts['recon_default'],
                self.opts['recon_default'] + self.opts['recon_max'],
                recon_delay)
            )

        log.debug("Setting zmq_reconnect_ivl to '{0}ms'".format(recon_delay))
        self._socket.setsockopt(zmq.RECONNECT_IVL, recon_delay)

        if hasattr(zmq, 'RECONNECT_IVL_MAX'):
            log.debug("Setting zmq_reconnect_ivl_max to '{0}ms'".format(
                self.opts['recon_default'] + self.opts['recon_max'])
            )

            self._socket.setsockopt(
                zmq.RECONNECT_IVL_MAX, self.opts['recon_max']
            )

        if self.opts['ipv6'] is True and hasattr(zmq, 'IPV4ONLY'):
            # IPv6 sockets work for both IPv6 and IPv4 addresses
            self._socket.setsockopt(zmq.IPV4ONLY, 0)

    def destroy(self):
        if hasattr(self, '_stream'):
            # TODO: Optionally call stream.close() on newer pyzmq? Its broken on some
            self._stream.io_loop.remove_handler(self._stream.socket)
            self._stream.socket.close(0)
        self.context.term()

    def __del__(self):
        self.destroy()

    # TODO: this is the time to see if we are connected, maybe use the req channel to guess?
    @tornado.gen.coroutine
    def connect(self):
        if not self.auth.authenticated:
            yield self.auth.authenticate()
        self.publish_port = self.auth.creds['publish_port']
        self._socket.connect(self.master_pub)

    @property
    def master_pub(self):
        '''
        Return the master publish port
        '''
        return 'tcp://{ip}:{port}'.format(ip=self.opts['master_ip'],
                                          port=self.publish_port)

    @tornado.gen.coroutine
    def _decode_messages(self, messages):
        '''
        Take the zmq messages, decrypt/decode them into a payload
        '''
        messages_len = len(messages)
        # if it was one message, then its old style
        if messages_len == 1:
            payload = self.serial.loads(messages[0])
        # 2 includes a header which says who should do it
        elif messages_len == 2:
            payload = self.serial.loads(messages[1])
        else:
            raise Exception(('Invalid number of messages ({0}) in zeromq pub'
                             'message from master').format(len(messages_len)))
        ret = yield self._decode_payload(payload)
        raise tornado.gen.Return(ret)

    @property
    def stream(self):
        if not hasattr(self, '_stream'):
            self._stream = zmq.eventloop.zmqstream.ZMQStream(self._socket, io_loop=self.io_loop)
        return self._stream

    def on_recv(self, callback):
        '''
        Register a callback for recieved messages (that we didn't initiate)
        '''
        if callback is None:
            return self.stream.on_recv(None)

        @tornado.gen.coroutine
        def wrap_callback(messages):
            payload = yield self._decode_messages(messages)
            callback(payload)
        return self.stream.on_recv(wrap_callback)


class ZeroMQReqServerChannel(salt.transport.mixins.auth.AESReqServerMixin, salt.transport.server.ReqServerChannel):
    def zmq_device(self):
        '''
        Multiprocessing target for the zmq queue device
        '''
        salt.utils.appendproctitle('MWorkerQueue')
        self.context = zmq.Context(self.opts['worker_threads'])
        # Prepare the zeromq sockets
        self.uri = 'tcp://{interface}:{ret_port}'.format(**self.opts)
        self.clients = self.context.socket(zmq.ROUTER)
        if self.opts['ipv6'] is True and hasattr(zmq, 'IPV4ONLY'):
            # IPv6 sockets work for both IPv6 and IPv4 addresses
            self.clients.setsockopt(zmq.IPV4ONLY, 0)

        self.workers = self.context.socket(zmq.DEALER)
        self.w_uri = 'ipc://{0}'.format(
            os.path.join(self.opts['sock_dir'], 'workers.ipc')
        )

        log.info('Setting up the master communication server')
        self.clients.bind(self.uri)

        self.workers.bind(self.w_uri)

        while True:
            try:
                zmq.device(zmq.QUEUE, self.clients, self.workers)
            except zmq.ZMQError as exc:
                if exc.errno == errno.EINTR:
                    continue
                raise exc

    def close(self):
        if hasattr(self, 'clients'):
            self.clients.close()
        self.stream.close()

    def pre_fork(self, process_manager):
        '''
        Pre-fork we need to create the zmq router device
        '''
        salt.transport.mixins.auth.AESReqServerMixin.pre_fork(self, process_manager)
        process_manager.add_process(self.zmq_device)

    def post_fork(self, payload_handler, io_loop):
        '''
        After forking we need to create all of the local sockets to listen to the
        router
        '''
        self.payload_handler = payload_handler
        self.io_loop = io_loop

        self.context = zmq.Context(1)
        self._socket = self.context.socket(zmq.REP)
        self.w_uri = 'ipc://{0}'.format(
            os.path.join(self.opts['sock_dir'], 'workers.ipc')
            )
        log.info('Worker binding to socket {0}'.format(self.w_uri))
        self._socket.connect(self.w_uri)

        salt.transport.mixins.auth.AESReqServerMixin.post_fork(self, payload_handler, io_loop)

        self.stream = zmq.eventloop.zmqstream.ZMQStream(self._socket, io_loop=self.io_loop)
        self.stream.on_recv_stream(self.handle_message)

    @tornado.gen.coroutine
    def handle_message(self, stream, payload):
        '''
        Handle incoming messages from underylying tcp streams
        '''
        try:
            payload = self.serial.loads(payload[0])
            payload = self._decode_payload(payload)
        except Exception as e:
            log.error('Bad load from minion')
            stream.send(self.serial.dumps('bad load'))
            raise tornado.gen.Return()

        # TODO helper functions to normalize payload?
        if not isinstance(payload, dict) or not isinstance(payload.get('load'), dict):
            log.error('payload and load must be a dict')
            stream.send(self.serial.dumps('payload and load must be a dict'))
            raise tornado.gen.Return()

        # intercept the "_auth" commands, since the main daemon shouldn't know
        # anything about our key auth
        if payload['enc'] == 'clear' and payload.get('load', {}).get('cmd') == '_auth':
            stream.send(self.serial.dumps(self._auth(payload['load'])))
            raise tornado.gen.Return()

        # TODO: test
        try:
            ret, req_opts = yield self.payload_handler(payload)
        except Exception as e:
            # always attempt to return an error to the minion
            stream.send('Some exception handling minion payload')
            log.error('Some exception handling a payload from minion', exc_info=True)
            raise tornado.gen.Return()

        req_fun = req_opts.get('fun', 'send')
        if req_fun == 'send_clear':
            stream.send(self.serial.dumps(ret))
        elif req_fun == 'send':
            stream.send(self.serial.dumps(self.crypticle.dumps(ret)))
        elif req_fun == 'send_private':
            stream.send(self.serial.dumps(self._encrypt_private(ret,
                                                                req_opts['key'],
                                                                req_opts['tgt'],
                                                                )))
        else:
            log.error('Unknown req_fun {0}'.format(req_fun))
            # always attempt to return an error to the minion
            stream.send('Server-side exception handling payload')
        raise tornado.gen.Return()


class ZeroMQPubServerChannel(salt.transport.server.PubServerChannel):
    def __init__(self, opts):
        self.opts = opts
        self.serial = salt.payload.Serial(self.opts)  # TODO: in init?

    def connect(self):
        return tornado.gen.sleep(5)

    def _publish_daemon(self):
        '''
        Bind to the interface specified in the configuration file
        '''
        salt.utils.appendproctitle(self.__class__.__name__)
        # Set up the context
        context = zmq.Context(1)
        # Prepare minion publish socket
        pub_sock = context.socket(zmq.PUB)
        # if 2.1 >= zmq < 3.0, we only have one HWM setting
        try:
            pub_sock.setsockopt(zmq.HWM, self.opts.get('pub_hwm', 1000))
        # in zmq >= 3.0, there are separate send and receive HWM settings
        except AttributeError:
            pub_sock.setsockopt(zmq.SNDHWM, self.opts.get('pub_hwm', 1000))
            pub_sock.setsockopt(zmq.RCVHWM, self.opts.get('pub_hwm', 1000))
        if self.opts['ipv6'] is True and hasattr(zmq, 'IPV4ONLY'):
            # IPv6 sockets work for both IPv6 and IPv4 addresses
            pub_sock.setsockopt(zmq.IPV4ONLY, 0)
        pub_uri = 'tcp://{interface}:{publish_port}'.format(**self.opts)
        # Prepare minion pull socket
        pull_sock = context.socket(zmq.PULL)
        pull_uri = 'ipc://{0}'.format(
            os.path.join(self.opts['sock_dir'], 'publish_pull.ipc')
        )
        salt.utils.zeromq.check_ipc_path_max_len(pull_uri)

        # Start the minion command publisher
        log.info('Starting the Salt Publisher on {0}'.format(pub_uri))
        pub_sock.bind(pub_uri)

        # Securely create socket
        log.info('Starting the Salt Puller on {0}'.format(pull_uri))
        old_umask = os.umask(0o177)
        try:
            pull_sock.bind(pull_uri)
        finally:
            os.umask(old_umask)

        try:
            while True:
                # Catch and handle EINTR from when this process is sent
                # SIGUSR1 gracefully so we don't choke and die horribly
                try:
                    package = pull_sock.recv()
                    unpacked_package = salt.payload.unpackage(package)
                    payload = unpacked_package['payload']
                    if self.opts['zmq_filtering']:
                        # if you have a specific topic list, use that
                        if 'topic_lst' in unpacked_package:
                            for topic in unpacked_package['topic_lst']:
                                # zmq filters are substring match, hash the topic
                                # to avoid collisions
                                htopic = hashlib.sha1(topic).hexdigest()
                                pub_sock.send(htopic, flags=zmq.SNDMORE)
                                pub_sock.send(payload)
                                # otherwise its a broadcast
                        else:
                            # TODO: constants file for "broadcast"
                            pub_sock.send('broadcast', flags=zmq.SNDMORE)
                            pub_sock.send(payload)
                    else:
                        pub_sock.send(payload)
                except zmq.ZMQError as exc:
                    if exc.errno == errno.EINTR:
                        continue
                    raise exc

        except KeyboardInterrupt:
            if pub_sock.closed is False:
                pub_sock.setsockopt(zmq.LINGER, 1)
                pub_sock.close()
            if pull_sock.closed is False:
                pull_sock.setsockopt(zmq.LINGER, 1)
                pull_sock.close()
            if context.closed is False:
                context.term()

    def pre_fork(self, process_manager):
        '''
        Do anything necessary pre-fork. Since this is on the master side this will
        primarily be used to create IPC channels and create our daemon process to
        do the actual publishing
        '''
        process_manager.add_process(self._publish_daemon)

    def publish(self, load):
        '''
        Publish "load" to minions
        '''
        payload = {'enc': 'aes'}

        crypticle = salt.crypt.Crypticle(self.opts, salt.master.SMaster.secrets['aes']['secret'].value)
        payload['load'] = crypticle.dumps(load)
        if self.opts['sign_pub_messages']:
            master_pem_path = os.path.join(self.opts['pki_dir'], 'master.pem')
            log.debug("Signing data packet")
            payload['sig'] = salt.crypt.sign_message(master_pem_path, payload['load'])
        # Send 0MQ to the publisher
        context = zmq.Context(1)
        pub_sock = context.socket(zmq.PUSH)
        pull_uri = 'ipc://{0}'.format(
            os.path.join(self.opts['sock_dir'], 'publish_pull.ipc')
            )
        pub_sock.connect(pull_uri)
        int_payload = {'payload': self.serial.dumps(payload)}

        # add some targeting stuff for lists only (for now)
        if load['tgt_type'] == 'list':
            int_payload['topic_lst'] = load['tgt']

        pub_sock.send(self.serial.dumps(int_payload))


# TODO: unit tests!
class AsyncReqMessageClient(object):
    '''
    This class wraps the underylying zeromq REQ socket and gives a future-based
    interface to sending and recieving messages. This works around the primary
    limitation of serialized send/recv on the underlying socket by queueing the
    message sends in this class. In the future if we decide to attempt to multiplex
    we can manage a pool of REQ/REP sockets-- but for now we'll just do them in serial
    '''
    def __init__(self, opts, addr, linger=0, io_loop=None):
        self.opts = opts
        self.addr = addr
        self.linger = linger
        self.io_loop = io_loop or zmq.eventloop.ioloop.ZMQIOLoop.current()

        self.serial = salt.payload.Serial(self.opts)

        self.context = zmq.Context()

        # wire up sockets
        self._init_socket()

        self.send_queue = []
        # mapping of message -> future
        self.send_future_map = {}

        self.send_timeout_map = {}  # message -> timeout

    # TODO: timeout all in-flight sessions, or error
    def destroy(self):
        if hasattr(self, 'stream'):
            # TODO: Optionally call stream.close() on newer pyzmq? Its broken on some
            self.stream.io_loop.remove_handler(self.stream.socket)
            self.stream.socket.close()
            self.socket.close()
        self.context.term()

    def __del__(self):
        self.destroy()

    def _init_socket(self):
        if hasattr(self, 'stream'):
            self.stream.close()  # pylint: disable=E0203
            self.socket.close()  # pylint: disable=E0203
            del self.stream
            del self.socket

        self.socket = self.context.socket(zmq.REQ)

        # socket options
        if hasattr(zmq, 'RECONNECT_IVL_MAX'):
            self.socket.setsockopt(
                zmq.RECONNECT_IVL_MAX, 5000
            )

        self._set_tcp_keepalive()
        if self.addr.startswith('tcp://['):
            # Hint PF type if bracket enclosed IPv6 address
            if hasattr(zmq, 'IPV6'):
                self.socket.setsockopt(zmq.IPV6, 1)
            elif hasattr(zmq, 'IPV4ONLY'):
                self.socket.setsockopt(zmq.IPV4ONLY, 0)
        self.socket.linger = self.linger
        self.socket.connect(self.addr)
        self.stream = zmq.eventloop.zmqstream.ZMQStream(self.socket, io_loop=self.io_loop)

    def _set_tcp_keepalive(self):
        if hasattr(zmq, 'TCP_KEEPALIVE') and self.opts:
            if 'tcp_keepalive' in self.opts:
                self.socket.setsockopt(
                    zmq.TCP_KEEPALIVE, self.opts['tcp_keepalive']
                )
            if 'tcp_keepalive_idle' in self.opts:
                self.socket.setsockopt(
                    zmq.TCP_KEEPALIVE_IDLE, self.opts['tcp_keepalive_idle']
                )
            if 'tcp_keepalive_cnt' in self.opts:
                self.socket.setsockopt(
                    zmq.TCP_KEEPALIVE_CNT, self.opts['tcp_keepalive_cnt']
                )
            if 'tcp_keepalive_intvl' in self.opts:
                self.socket.setsockopt(
                    zmq.TCP_KEEPALIVE_INTVL, self.opts['tcp_keepalive_intvl']
                )

    @tornado.gen.coroutine
    def _internal_send_recv(self):
        while len(self.send_queue) > 0:
            message = self.send_queue.pop(0)
            future = self.send_future_map[message]

            # send
            def mark_future(msg):
                if not future.done():
                    future.set_result(self.serial.loads(msg[0]))
            self.stream.on_recv(mark_future)
            self.stream.send(message)

            try:
                ret = yield future
            except:  # pylint: disable=W0702
                self._init_socket()  # re-init the zmq socket (no other way in zmq)
                continue
            self.remove_message_timeout(message)

    def remove_message_timeout(self, message):
        if message not in self.send_timeout_map:
            return
        timeout = self.send_timeout_map.pop(message)
        self.io_loop.remove_timeout(timeout)

    def timeout_message(self, message):
        del self.send_timeout_map[message]
        self.send_future_map.pop(message).set_exception(SaltReqTimeoutError('Message timed out'))

    def send(self, message, timeout=None, callback=None):
        '''
        Return a future which will be completed when the message has a response
        '''
        message = self.serial.dumps(message)
        future = tornado.concurrent.Future()
        if callback is not None:
            def handle_future(future):
                response = future.result()
                self.io_loop.add_callback(callback, response)
            future.add_done_callback(handle_future)
        # Add this future to the mapping
        self.send_future_map[message] = future

        if timeout is not None:
            send_timeout = self.io_loop.call_later(timeout, self.timeout_message, message)
            self.send_timeout_map[message] = send_timeout

        if len(self.send_queue) == 0:
            self.io_loop.spawn_callback(self._internal_send_recv)

        self.send_queue.append(message)

        return future
