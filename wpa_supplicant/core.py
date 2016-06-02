# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Copyright (c) 2015 Digi International Inc. All Rights Reserved.

"""

    This module (library) is an intuitive wrapper around wpa_supplicant's D-Bus API

For the most part, the library aims to provide a one-to-one mapping into the D-Bus
interfaces offered by wpa_supplicant.  Certain abstractions or niceties have been made
to make the usage of the API more intuitive.

The libraries API was mainly derived from the following documentation:

    http://w1.fi/wpa_supplicant/devel/dbus.html

"""
from twisted.internet import defer, threads
from txdbus.interface import DBusInterface, Method, Signal
from txdbus.marshal import ObjectPath
from txdbus import client, error
from functools import wraps
import dbus
import threading
import logging

#
# Constants/Globals
#
BUS_NAME = 'fi.w1.wpa_supplicant1'
logger = logging.getLogger('wpasupplicant')


#
# Helpers
#
def _catch_remote_errors(fn):
    """Decorator for catching and wrapping txdbus.error.RemoteError exceptions"""
    @wraps(fn)
    def closure(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except error.RemoteError as ex:
            match = _REMOTE_EXCEPTIONS.get(str(ex.errName))
            if match is not None:
                raise match(ex.message)
            else:
                logger.warn("Unrecognized error: %s" % ex.errName)
                raise WpaSupplicantException(ex.message)
    return closure


def _eval(deferred, reactor):
        """Evaluate a deferred on a given reactor and return the result

        This function is safe to call with a deferred that has already been evaluated.
        """

        @defer.inlineCallbacks
        def closure():
            if deferred.called:
                result = deferred.result
            else:
                result = yield deferred

            defer.returnValue(result)

        if threading.currentThread().getName() == reactor.thread_name:
            return closure()
        else:
            return threads.blockingCallFromThread(reactor, closure)


#
# Exceptions
#
class WpaSupplicantException(Exception):
    """Base class for all exception types raised from this lib"""


class UnknownError(WpaSupplicantException):
    """Something failed for an unknown reason

    Possible sources:
        Basically everything in this library
    """


class MethodTimeout(WpaSupplicantException):
    """Raised when a timeout occurs while waiting on DeferredQueue.get()

    Possible sources:
        :meth:`~Interface.scan` with block=True
    """


class InterfaceExists(WpaSupplicantException):
    """wpa_supplicant already controls this interface

    Possible sources:
        :meth:`~WpaSupplicant.create_interface`
    """


class InvalidArgs(WpaSupplicantException):
    """Invalid entries were found in the passed arguments
    
    Possible sources:
        :meth:`~WpaSupplicant.create_interface`
        :meth:`~Interface.scan`
        :meth:`~Interface.add_network`
        :meth:`~Interface.remove_network`
        :meth:`~Interface.select_network`
    """


class InterfaceUnknown(WpaSupplicantException):
    """Object pointed by the path doesn't exist or doesn't represent an interface
    
    Possible sources:
        :meth:`~WpaSupplicant.get_interface`
        :meth:`~WpaSupplicant.remove_interface`
    """


class NotConnected(WpaSupplicantException):
    """Interface is not connected to any network

    Possible sources:
        :meth:`~Interface.disconnect`
    """


class NetworkUnknown(WpaSupplicantException):
    """A passed path doesn't point to any network object

    Possible sources:
        :meth:`~Interface.remove_network`
        :meth:`~Interface.select_network`
    """


class ReactorNotRunning(WpaSupplicantException):
    """In order to connect to the WpaSupplicantDriver a reactor must be started"""


# These are exceptions defined by the wpa_supplicant D-Bus API
_REMOTE_EXCEPTIONS = {
    'fi.w1.wpa_supplicant1.UnknownError': UnknownError,
    'fi.w1.wpa_supplicant1.InvalidArgs': InvalidArgs,
    'fi.w1.wpa_supplicant1.InterfaceExists': InterfaceExists,
    'fi.w1.wpa_supplicant1.InterfaceUnknown': InterfaceUnknown,
    'fi.w1.wpa_supplicant1.NotConnected': NotConnected,
    'fi.w1.wpa_supplicant1.NetworkUnknown': NetworkUnknown,
}


#
# Library Core
#
class TimeoutDeferredQueue(defer.DeferredQueue):
    """Deferred Queue implementation that provides .get() a timeout keyword arg"""

    def __init__(self, reactor):
        defer.DeferredQueue.__init__(self)
        self._reactor = reactor

    def _timed_out(self, deferred):
        """A timeout occurred while waiting for the deferred to be fired"""

        if not deferred.called:
            if deferred in self.waiting:
                self.waiting.remove(deferred)
                deferred.errback(MethodTimeout('Timed out waiting for response'))

    def _stop_timeout(self, result, call_id):
        """One of the callbacks in the deferreds callback-chain"""

        call_id.cancel()
        return result

    def get(self, timeout=None):
        """Synchronously get the result of :meth:`defer.DeferredQueue.get()`

        :param timeout: optional timeout parameter (integer # of sections)
        :returns: The result value that has been put onto the deferred queue
        :raises MethodTimeout: More time has elapsed than the specified `timeout`
        """

        deferred = defer.DeferredQueue.get(self)

        if timeout:
            call_id = self._reactor.callLater(timeout, self._timed_out, deferred)
            deferred.addCallback(self._stop_timeout, call_id)

        return _eval(deferred, self._reactor)


class RemoteSignal(object):
    """Encapsulation of a signal object returned by signal registration methods"""

    def __init__(self, signal_name, callback, remote_object, remote_interface, reactor):
        self._signal_name = signal_name
        self._callback = callback
        self._remote_object = remote_object
        self._remote_interface = remote_interface
        self._reactor = reactor
        self._deferred = remote_object.notifyOnSignal(signal_name, callback, remote_interface)

    def get_signal_name(self):
        return self._signal_name

    def cancel(self):
        self._remote_object.cancelSignalNotification(_eval(self._deferred, self._reactor))


class BaseIface(object):
    """Base class for all Interfaces defined in the wpa_supplicant D-Bus API"""

    def __init__(self, obj_path, conn, reactor):
        self._obj_path = obj_path
        self._conn = conn
        self._reactor = reactor
        self._introspection = _eval(self._conn.getRemoteObject(BUS_NAME, self._obj_path),
                                    self._reactor)
        self._without_introspection = _eval(self._conn.getRemoteObject(BUS_NAME, self._obj_path, self.iface),
                                            self._reactor)

    def get_path(self):
        """Return the full D-Bus object path that defines this interface

        :returns: String D-Bus object path, e.g. /w1/fi/wpa_supplicant1/Interfaces/0
        :rtype: str
        """

        return self._obj_path

    @_catch_remote_errors
    def _call_remote(self, method, *args):
        """Call a remote D-Bus interface method using introspection"""

        return _eval(self._introspection.callRemote(method, *args), self._reactor)

    @_catch_remote_errors
    def _call_remote_without_introspection(self, method, *args):
        """Call a remote D-Bus interface method without using introspection"""

        return _eval(self._without_introspection.callRemote(method, *args), self._reactor)

    @_catch_remote_errors
    def register_signal_once(self, signal_name):
        """Register a signal on an Interface object

        .. note::

            Since this de-registers itself after the signal fires the only real
            use for this function is code that wants to block and wait for a signal
            to fire, hence the queue it returns.

        :param signal_name: Case-sensitive name of the signal
        :returns: Object of type :class:`~EventSignal`
        """

        q = TimeoutDeferredQueue(self._reactor)

        signal = None

        def signal_callback(result):
            q.put(result)
            if signal is not None:
                signal.cancel()

        signal = RemoteSignal(signal_name,
                              signal_callback,
                              self._introspection,
                              self.INTERFACE_PATH,
                              self._reactor)

        return q

    @_catch_remote_errors
    def register_signal(self, signal_name, callback):
        """Register a callback when a signal fires

        :param signal_name: Case-sensitve name of the signal
        :param callback: Callable object
        :returns: ~`RemoteSignal`
        """
        if not callable(callback):
            raise WpaSupplicantException('callback must be callable')

        return RemoteSignal(signal_name,
                            callback,
                            self._introspection,
                            self.INTERFACE_PATH,
                            self._reactor)

    @_catch_remote_errors
    def get(self, property_name):
        """Get the value of a property defined by the interface

        :param property_name: Case-sensitive string of the property name
        :returns: Variant type of the properties value
        """
        return self._call_remote('Get', self.INTERFACE_PATH, property_name)

    @_catch_remote_errors
    def set(self, property_name, value):
        """Set the value of a property defined by the interface

        :param property_name: Case-sensitive string of the property name
        :param value: Variant type value to set for property
        :returns: `None`
        """

        logger.info('Setting `%s` -> `%s`', property_name, value)
        return self._call_remote('Set', self.INTERFACE_PATH, property_name, value)


class WpaSupplicant(BaseIface):
    """Interface implemented by the main wpa_supplicant D-Bus object

            Registered in the bus as "fi.w1.wpa_supplicant1"
    """

    INTERFACE_PATH = 'fi.w1.wpa_supplicant1'

    iface = DBusInterface(
        INTERFACE_PATH,
        Method('CreateInterface', arguments='a{sv}', returns='o'),
        Method('GetInterface', arguments='s', returns='o'),
        Method('RemoveInterface', arguments='o'),
        Signal('InterfaceAdded', 'o,a{sv}'),
        Signal('InterfaceRemoved', 'o'),
        Signal('PropertiesChanged', 'a{sv}')
    )

    def __init__(self, *args, **kwargs):
        BaseIface.__init__(self, *args, **kwargs)
        self._interfaces_cache = dict()

    def __repr__(self):
        return str(self)

    def __str__(self):
        return "WpaSupplicant(Interfaces: %s)" % self.get_interfaces()

    #
    # Methods
    #
    def get_interface(self, interface_name):
        """Get D-Bus object related to an interface which wpa_supplicant already controls

        :returns: Interface object that implements the wpa_supplicant Interface API.
        :rtype: :class:`~Interface`
        :raises InterfaceUnknown: wpa_supplicant doesn't know anything about `interface_name`
        :raises UnknownError: An unknown error occurred
        """

        interface_path = self._call_remote_without_introspection('GetInterface',
                                                                 interface_name)
        interface = self._interfaces_cache.get(interface_path, None)
        if interface is not None:
            return interface
        else:
            interface = Interface(interface_path, self._conn, self._reactor)
            self._interfaces_cache[interface_path] = interface
            return interface

    def create_interface(self, interface_name):
        """Registers a wireless interface in wpa_supplicant

        :returns: Interface object that implements the wpa_supplicant Interface API
        :raises InterfaceExists: The `interface_name` specified is already registered
        :raises UnknownError: An unknown error occurred
        """

        interface_path = self._call_remote_without_introspection('CreateInterface',
                                                                 {'Ifname': interface_name})
        return Interface(interface_path, self._conn, self._reactor)

    def remove_interface(self, interface_path):
        """Deregisters a wireless interface from wpa_supplicant

        :param interface_path: D-Bus object path to the interface to be removed
        :returns: None
        :raises InterfaceUnknown: wpa_supplicant doesn't know anything about `interface_name`
        :raises UnknownError: An unknown error occurred
        """

        self._call_remote_without_introspection('RemoveInterface', interface_path)

    #
    # Properties
    #
    def get_debug_level(self):
        """Global wpa_supplicant debugging level

        :returns:  Possible values: "msgdump" (verbose debugging)
                                    "debug" (debugging)
                                    "info" (informative)
                                    "warning" (warnings)
                                    "error" (errors)
        :rtype: str
        """

        return self.get('DebugLevel')

    def get_debug_timestamp(self):
        """ Determines if timestamps are shown in debug logs"""

        return self.get('DebugTimestamp')

    def get_debug_showkeys(self):
        """Determines if secrets are shown in debug logs"""

        return self.get('DebugShowKeys')

    def get_interfaces(self):
        """An array with paths to D-Bus objects representing controlled interfaces"""

        return self.get('Interfaces')

    def get_eap_methods(self):
        """An array with supported EAP methods names"""

        return self.get('EapMethods')


class Interface(BaseIface):
    """Interface implemented by objects related to network interface added to wpa_supplicant"""

    INTERFACE_PATH = 'fi.w1.wpa_supplicant1.Interface'

    iface = DBusInterface(
        INTERFACE_PATH,
        Method('Scan', arguments='a{sv}'),
        Method('AddNetwork', arguments='a{sv}', returns='o'),
        Method('RemoveNetwork', arguments='o'),
        Method('SelectNetwork', arguments='o'),
        Method('Disconnect'),
        Signal('ScanDone', 'b'),
        Signal('PropertiesChanged', 'a{sv}')
    )

    def __repr__(self):
        return str(self)

    def __str__(self):
        return "Interface(Path: %s, Name: %s, State: %s)" % (self.get_path(),
                                                             self.get_ifname(),
                                                             self.get_state())

    #
    # Methods
    #
    def scan(self, type='active', ssids=None, ies=None, channels=None, block=False):
        """Triggers a scan

        :returns: List of `BSS` objects if block=True else None
        :raises InvalidArgs: Invalid argument format
        :raises MethodTimeout: Scan has timed out (only if block=True)
        :raises UnknownError: An unknown error occurred
        """

        # TODO: Handle the other arguments
        scan_options = {
            'Type': type
        }

        # If blocking, register for the ScanDone signal and return BSSs
        if block:
            deferred_queue = self.register_signal_once('ScanDone')
            self._call_remote('Scan', scan_options)  # Trigger scan
            success = deferred_queue.get(timeout=10)
            if success:
                return [BSS(path, self._conn, self._reactor) for path in self.get_all_bss()]
            else:
                raise UnknownError('ScanDone signal received without success')
        else:
            self._call_remote('Scan', scan_options)  # Trigger scan

    def add_network(self, network_cfg):
        """Adds a new network to the interface

        :param network_cfg: Dictionary of config, see wpa_supplicant.conf for k/v pairs
        :returns: `Network` object that was registered w/ wpa_supplicant
        :raises InvalidArgs: Invalid argument format
        :raises UnknownError: An unknown error occurred
        """

        network_path = self._call_remote('AddNetwork', network_cfg)
        return Network(network_path, self._conn, self._reactor)

    def remove_network(self, network_path):
        """Removes a configured network from the interface

        :param network_path: D-Bus object path to the desired network
        :returns: None
        :raises NetworkUnknown: The specified `network_path` is invalid
        :raises InvalidArgs: Invalid argument format
        :raises UnknownError: An unknown error occurred
        """

        self._call_remote('RemoveNetwork', network_path)

    def select_network(self, network_path):
        """Attempt association with a configured network

        :param network_path: D-Bus object path to the desired network
        :returns: None
        :raises NetworkUnknown: The specified `network_path` has not been added
        :raises InvalidArgs: Invalid argument format
        """

        self._call_remote('SelectNetwork', network_path)

    def disconnect_network(self):
        """Disassociates the interface from current network

        :returns: None
        :raises NotConnected: The interface is not currently connected to a network
        """

        self._call_remote('Disconnect')

    #
    # Properties
    #
    def get_ifname(self):
        """Name of network interface controlled by the interface, e.g., wlan0"""

        return self.get('Ifname')

    def get_current_bss(self):
        """BSS object path which wpa_supplicant is associated with

                Returns "/" if is not associated at all
        """

        bss_path = self.get('CurrentBSS')
        if bss_path == '/' or bss_path is None:
            return None
        else:
            return BSS(bss_path, self._conn, self._reactor)

    def get_current_network(self):
        """The `Network` object which wpa_supplicant is associated with

                Returns `None` if is not associated at all
        """

        network_path = self.get('CurrentNetwork')
        if network_path == '/' or network_path is None:
            return None
        else:
            return Network(network_path, self._conn, self._reactor)

    def get_p2pdevice(self):
        return P2PDevice(self.get_path(), self._conn, self._reactor)

    def get_networks(self):
        """List of `Network` objects representing configured networks"""

        networks = list()
        paths = self.get('Networks')
        for network_path in paths:
            if network_path == '/':
                networks.append(None)
            else:
                networks.append(Network(network_path, self._conn, self._reactor))
        return networks

    def get_state(self):
        """A state of the interface.

            Possible values are: "disconnected"
                                 "inactive"
                                 "scanning"
                                 "authenticating"
                                 "associating"
                                 "associated"
                                 "4way_handshake"
                                 "group_handshake"
                                 "completed"
                                 "unknown"
        """

        return self.get('State')

    def get_scanning(self):
        """Determines if the interface is already scanning or not"""

        return self.get('Scanning')

    def get_scan_interval(self):
        """Time (in seconds) between scans for a suitable AP. Must be >= 0"""

        return self.get('ScanInterval')

    def get_fast_reauth(self):
        """Identical to fast_reauth entry in wpa_supplicant.conf"""

        return self.get('FastReauth')

    def get_all_bss(self):
        """List of D-Bus objects paths representing BSSs known to the interface"""

        return self.get('BSSs')

    def get_driver(self):
        """Name of driver used by the interface, e.g., nl80211"""

        return self.get('Driver')

    def get_country(self):
        """Identical to country entry in wpa_supplicant.conf"""

        return self.get('Country')

    def get_bridge_ifname(self):
        """Name of bridge network interface controlled by the interface, e.g., br0"""

        return self.get('BridgeIfname')

    def get_bss_expire_age(self):
        """Identical to bss_expiration_age entry in wpa_supplicant.conf file"""

        return self.get('BSSExpireAge')

    def get_bss_expire_count(self):
        """Identical to bss_expiration_scan_count entry in wpa_supplicant.conf file"""

        return self.get('BSSExpireCount')

    def get_ap_scan(self):
        """Identical to ap_scan entry in wpa_supplicant configuration file.

                Possible values are 0, 1 or 2.
        """

        return self.get('ApScan')

    def set_country(self, country_code):
        self.set('Country', country_code)


class P2PDevice(BaseIface):
    """Interface implemented by objects related to P2P (Wifi Direct)"""

    INTERFACE_PATH = 'fi.w1.wpa_supplicant1.Interface.P2PDevice'

    iface = DBusInterface(
        INTERFACE_PATH,
        Method('Find', arguments='a{sv}'),
        Method('StopFind'),
        Method('Listen', arguments='i'),
        Method('ExtendedListen', arguments='a{sv}'),
        Method('PresenceRequest', arguments='a{sv}'),
        Method('ProvisionDiscoveryRequest', arguments='os'),
        Method('Connect', arguments='a{sv}', returns='s'),
        Method('GroupAdd', arguments='a{sv}'),
        Method('Cancel'),
        Method('Invite', arguments='a{sv}'),
        Method('Disconnect'),
        Method('RejectPeer', arguments='o'),
        Method('RemoveClient', arguments='a{sv}'),
        Method('Flush'),
        Method('AddService', arguments='a{sv}'),
        Method('DeleteService', arguments='a{sv}'),
        Method('FlushService'),
        Method('ServiceDiscoveryRequest', arguments='a{sv}', returns='t'),
        Method('ServiceDiscoveryResponse', arguments='a{sv}'),
        Method('ServiceDiscoveryCancelRequest', arguments='t'),
        Method('ServiceUpdate'),
        Method('ServiceDiscoveryExternal', arguments='i'),
        Method('AddPersistentGroup', arguments='a{sv}', returns='o'),
        Method('RemovePersistentGroup', arguments='o'),
        Method('RemoveAllPersistentGroups')
    )

    def __repr__(self):
        return str(self)

    def __str__(self):
        return "P2PDevice(Path: %s)" % (self.get_path())

    def _get_dbus_propery_interface(self):
        bus = dbus.SystemBus()
        proxy = bus.get_object(WpaSupplicant.INTERFACE_PATH, self.get_path())
        return dbus.Interface(proxy,
                              'org.freedesktop.DBus.Properties')


    #
    # Methods
    #
    def find(self,
             timeout=None,
             requested_device_types=None,
             discovery_type='start_with_full'):
        """Start P2P find operation (i.e., alternating P2P Search and Listen
        states to discover peers and be discoverable)

        timeout -- Timeout for operating in seconds
        requested_device_types -- WPS Device Types to search for (List of byte
                                  arrays with length 8)
        discovery_type -- "start_with_full" (default, if not specified),
                          "social", "progressive"
        """

        kwargs = {'DiscoveryType': discovery_type}

        if timeout is not None:
            kwargs['Timeout'] = timeout

        if requested_device_types is not None:
            kwargs['RequestedDeviceTypes'] = requested_device_types

        self._call_remote('Find', kwargs)

    def stop_find(self):
        """Stop P2P find operation."""
        self._call_remote('StopFind')

    def listen(self, timeout):
        """Start P2P listen operation (i.e., be discoverable).

        timeout -- Listen timeout in milliseconds
        :raises InvalidArgs: Invalid argument format
        :raises UnknownError: An unknown error occurred
        """

        self._call_remote('Listen', timeout)

    def extended_listen(self, period=None, interval=None):
        """Configure Extended Listen Timing. If the parameters are omitted,
        this feature is disabled. If the parameters are included, Listen State
        will be entered every interval msec for at least period msec. Both
        values have acceptable range of 1-65535 (with interval obviously having
        to be larger than or equal to duration). If the P2P module is not idle
        at the time the Extended Listen Timing timeout occurs, the Listen State
        operation will be skipped.

        period -- Extended listen period in milliseconds; 1-65535.
        interval -- Extended listen interval in milliseconds; 1-65535.
        """

        kwargs = {}
        if period is not None:
            kwargs['period'] = period
        if interval is not None:
            kwargs['interval'] = interval

        self._call_remote('ExtendedListen', kwargs)

    def presence_request(self,
                         duration1=None, interval1=None,
                         duration2=None, interval2=None):
        """Request a specific GO presence in a P2P group where the local device
        is a P2P Client. Send a P2P Presence Request to the GO (this is only
        available when acting as a P2P client). If no duration/interval pairs
        are given, the request indicates that this client has no special needs
        for GO presence. The first parameter pair gives the preferred duration
        and interval values in microseconds. If the second pair is included,
        that indicates which value would be acceptable.

        Note: This needs to be issued on a P2P group interface if separate
        group interfaces are used.

        duration1 -- Duration in microseconds.
        interval1 -- Interval in microseconds.
        duration2 -- Duration in microseconds.
        interval2 -- Interval in microseconds.
        """

        kwargs = {}
        if duration1 is not None:
            kwargs['duration1'] = duration1
        if interval1 is not None:
            kwargs['interval1'] = interval1
        if duration2 is not None:
            kwargs['duration2'] = duration2
        if interval2 is not None:
            kwargs['interval2'] = interval2

        self._call_remote('PresenceRequest', kwargs)

    def provision_discovery_request(self, peer, config_method):
        self._call_remote('ProvisionDiscoveryRequest', (peer, config_method))

    def connect(self, peer, wps_method, persistent=None, join=None,
                authorize_only=None, frequency=None, go_intent=None, pin=None):
        """Request a P2P group to be started through GO Negotiation or by
        joining an already operating group.

        peer -- Must be a syntactically valid object path of 'peer'
        wps_method -- "pbc", "display", "keypad", "pin" (alias for "display")
        persistent -- Whether to form a persistent group.
        join -- Whether to join an already operating group instead of forming a new group.
        authorize_only -- Whether to authorize a peer to initiate GO Negotiation instead of initiating immediately.
        frequency -- Operating frequency in MHz
        go_intent -- GO intent 0-15
        pin -- PIN to use

        :returns: generated_pin
        """

        kwargs = {'peer': ObjectPath(peer), 'wps_method': wps_method}
        if persistent is not None:
            kwargs['persistent'] = persistent
        if join is not None:
            kwargs['join'] = join
        if authorize_only is not None:
            kwargs['authorize_only'] = authorize_only
        if frequency is not None:
            kwargs['frequency'] = frequency
        if go_intent is not None:
            kwargs['go_intent'] = go_intent
        if pin is not None:
            kwargs['pin'] = pin

        return self._call_remote('Connect', kwargs)

    def group_add(self,
                  persistent=None,
                  persistent_group_object=None,
                  frequency=None):
        """Request a P2P group to be started without GO Negotiation.

        persistent --   Whether to form a persistent group.
        persistent_group_object -- Must be a syntactically valid object path of
                                   'persistent_group_object'.
        frequency -- Operating frequency in MHz
        """

        kwargs = {}
        if persistent is not None:
            kwargs['persistent'] = persistent
        if persistent_group_object is not None:
            kwargs['persistent_group_object'] = persistent_group_object
        if frequency is not None:
            kwargs['frequency'] = frequency

        self._call_remote('GroupAdd', kwargs)

    def cancel(self):
        """Stop ongoing P2P group formation operation."""
        self._call_remote('Cancel')

    def invite(self, peer, persistent_group_object=None):
        """Invite a peer to join an already operating group or to re-invoke a
        persistent group.

        peer -- Must be a syntactically valid object path of 'peer'
        persistent_group_object -- Must be a syntactically valid object path of
                                   'persistent_group_object'.
        """

        kwargs = {'peer': ObjectPath(peer)}
        if persistent_group_object is not None:
            kwargs['persistent_group_object'] = ObjectPath(
                persistent_group_object)

        self._call_remote('Invite', kwargs)

    def disconnect(self):
        """Terminate a P2P group.

        Note: This needs to be issued on a P2P group interface if separate
        group interfaces are used.
        """
        self._call_remote('Disconnect')

    def reject_peer(self, peer):
        """Reject connection attempt from a peer (specified with a device
        address). This is a mechanism to reject a pending GO Negotiation with a
        peer and request to automatically block any further connection or
        discovery of the peer.

        peer -- Must be a syntactically valid object path of 'peer'
        """

        self._call_remote('RejectPeer', peer)

    def remove_client(self, peer=None, iface=None):
        """Remove the client from all groups (operating and persistent) from
        the local GO.

        peer -- Object path for peer's P2P Device address
        iface -- Interface address[MAC Address format] of the peer to be
                 disconnected. Required if object path is not provided.
        """

        kwargs = {}
        if peer is not None:
            kwargs['peer'] = ObjectPath(peer)
        if iface is not None:
            kwargs['iface'] = iface

        self._call_remote('RemoveClient', kwargs)

    def flush(self):
        """Flush P2P peer table and state."""
        self._call_remote('Flush')

    def add_service(self, service_type,
                    version=None, service=None, query=None, response=None):
        """
        service_type -- "upnp" or "bonjour"
        version -- Required for UPnP services.
        """

        kwargs = {'service_type': service_type}
        if version is not None:
            kwargs['version'] = version
        if service is not None:
            kwargs['service'] = service
        if query is not None:
            kwargs['query'] = query
        if response is not None:
            kwargs['response'] = response

        self._call_remote('AddService', kwargs)

    def delete_service(self, service_type,
                       version=None, service=None, query=None):
        """
        service_type -- "upnp" or "bonjour"
        version -- Required for UPnP services.
        """

        kwargs = {'service_type': service_type}
        if version is not None:
            kwargs['version'] = version
        if service is not None:
            kwargs['service'] = service
        if query is not None:
            kwargs['query'] = query

        self._call_remote('DeleteService', kwargs)

    def flush_service(self):
        self._call_remote('FlushService')

    def service_discovery_request(self, peer_object=None, service_type=None,
                                  version=None, service=None, tlv=None):
        """
        service_type -- "upnp" or "bonjour"
        version -- Required for UPnP services.
        """

        kwargs = {}
        if peer_object is not None:
            kwargs['peer_object'] = ObjectPath(peer_object)
        if service_type is not None:
            kwargs['service_type'] = service_type
        if version is not None:
            kwargs['version'] = version
        if service is not None:
            kwargs['service'] = service
        if tlv is not None:
            kwargs['tlv'] = tlv

        return self._call_remote('ServiceDiscoveryRequest', kwargs)

    def service_discovery_response(
            self, peer_object, frequency, dialog_token, tlvs):

        kwargs = {'peer_object': ObjectPath(peer_object),
                  'frequency': frequency,
                  'dialog_token': dialog_token,
                  'tlvs': tlvs}

        self._call_remote('ServiceDiscoveryResponse', kwargs)

    def service_discovery_cancel_request(self, dialog_token):
        self._call_remote('ServiceDiscoveryCancelRequest', dialog_token)

    def service_update(self):
        self._call_remote('ServiceUpdate')

    def service_discovery_external(self, i):
        self._call_remote('ServiceDiscoveryExternal', i)

    def add_persistent_group(self, bssid, ssid, psk, mode):
        """
        bssid -- P2P Device Address of the GO in the persistent group.
        ssid -- SSID of the group
        psk -- Passphrase (on the GO and optionally on P2P Client) or PSK (on
               P2P Client if passphrase is not known)
        mode -- "3" on GO or "0" on P2P Client
        """

        kwargs = {'bssid': bssid, 'ssid': ssid, 'psk': psk, 'mode': mode}

        return self._call_remote('AddPersistentGroup', kwargs)

    def remove_persistent_group(self, path):
        self._call_remote('RemovePersistentGroup', path)

    def remove_all_persistent_groups(self):
        self._call_remote('RemoveAllPersistentGroups')

    #
    # Properties
    #
    def get_p2pdevice_config(self):
        """Dictionary containing P2P configuration"""

        return self.get('P2PDeviceConfig')

    def get_device_name(self):
        """Returns the device name"""

        return self.get_p2pdevice_config().get('DeviceName')

    def set_device_name(self, device_name):
        """Sets the device name"""
        argument = dbus.Dictionary(
            {dbus.String('DeviceName'): dbus.String(device_name)},
             signature=dbus.Signature('sv'),
             variant_level=1)

        iface = self._get_dbus_propery_interface()
        iface.Set(self.INTERFACE_PATH, 'P2PDeviceConfig', argument)

    def get_primary_device_type(self):
        """Returns the primary device type"""

        return self.get_p2pdevice_config().get('PrimaryDeviceType')

    def set_primary_device_type(self, primary_device_type):
        """Sets the primary device type. "primary_device_type" needs to be list
        of Bytes of length 8"""

        dbus_device_type = dbus.Array([dbus.Byte(x) for x in primary_device_type],
                                      signature=dbus.Signature('y'),
                                      variant_level=1)
        argument = dbus.Dictionary(
            {dbus.String('PrimaryDeviceType'): dbus_device_type},
             signature=dbus.Signature('sv'),
             variant_level=1)

        iface = self._get_dbus_propery_interface()
        iface.Set(self.INTERFACE_PATH, 'P2PDeviceConfig', argument)

    def get_secondary_device_types(self):
        """Returns the list of secondary device types"""

        return self.get_p2pdevice_config().get('SecondaryDeviceTypes')

    def set_secondary_device_types(self, secondary_device_types):
        """Sets the secondary device types. "secondary_device_types" needs to be list
        of device type, which in turn are a list of Bytes of length 8"""

        device_types = []
        for device_type in secondary_device_types:
            device_types.append(dbus.Array([dbus.Byte(x) for x in device_type],
                                            signature=dbus.Signature('y'),
                                            variant_level=1))

        dbus_device_types = dbus.Array(device_types,
                                       signature=dbus.Signature('ay'),
                                       variant_level=1)
        argument = dbus.Dictionary(
            {dbus.String('SecondaryDeviceTypes'): dbus_device_types},
             signature=dbus.Signature('sv'),
             variant_level=1)

        iface = self._get_dbus_propery_interface()
        iface.Set(self.INTERFACE_PATH, 'P2PDeviceConfig', argument)

    def get_vendor_extension(self):
        """Returns VendorExtension"""

        return self.get_p2pdevice_config().get('VendorExtension')

    def set_vendor_extension(self, vendor_extension):
        """Sets VendorExtension"""

        device_types = []
        for device_type in vendor_extension:
            device_types.append(dbus.Array([dbus.Byte(x) for x in device_type],
                                            signature=dbus.Signature('y'),
                                            variant_level=1))

        dbus_device_types = dbus.Array(device_types,
                                       signature=dbus.Signature('ay'),
                                       variant_level=1)
        argument = dbus.Dictionary(
            {dbus.String('VendorExtension'): dbus_device_types},
             signature=dbus.Signature('sv'),
             variant_level=1)

        iface = self._get_dbus_propery_interface()
        iface.Set(self.INTERFACE_PATH, 'P2PDeviceConfig', argument)

    def get_go_intent(self):
        """Returns the GO intent"""

        return self.get_p2pdevice_config().get('GOIntent')

    def set_go_intent(self, go_intent):
        """Sets the GO intent"""
        argument = dbus.Dictionary(
            {dbus.String('GOIntent'): dbus.UInt32(go_intent, variant_level=1)},
             signature=dbus.Signature('sv'),
             variant_level=1)

        iface = self._get_dbus_propery_interface()
        iface.Set(self.INTERFACE_PATH, 'P2PDeviceConfig', argument)

    def get_persistent_reconnect(self):
        """Returns True is persistent reconnect is enabled, false otherwise"""

        return self.get_p2pdevice_config().get('PersistentReconnect')

    def set_persistent_reconnect(self, persistent_reconnect):
        """Sets persistent reconnect"""
        argument = dbus.Dictionary(
            {dbus.String('PersistentReconnect'):
                dbus.Boolean(persistent_reconnect, variant_level=1)},
             signature=dbus.Signature('sv'),
             variant_level=1)

        iface = self._get_dbus_propery_interface()
        iface.Set(self.INTERFACE_PATH, 'P2PDeviceConfig', argument)

    def get_listen_reg_class(self):
        """Returns the ListenRegClass"""

        return self.get_p2pdevice_config().get('ListenRegClass')

    def set_listen_reg_class(self, listen_reg_class):
        """Sets the ListenRegClass"""
        argument = dbus.Dictionary(
            {dbus.String('ListenRegClass'):
                dbus.UInt32(listen_reg_class, variant_level=1)},
             signature=dbus.Signature('sv'),
             variant_level=1)

        iface = self._get_dbus_propery_interface()
        iface.Set(self.INTERFACE_PATH, 'P2PDeviceConfig', argument)

    def get_listen_channel(self):
        """Returns the channel on which the device is listening"""

        return self.get_p2pdevice_config().get('ListenChannel')

    def set_listen_channel(self, listen_channel):
        """Sets which channel the device is listening on"""
        argument = dbus.Dictionary(
            {dbus.String('ListenChannel'):
                dbus.UInt32(listen_channel, variant_level=1)},
             signature=dbus.Signature('sv'),
             variant_level=1)

        iface = self._get_dbus_propery_interface()
        iface.Set(self.INTERFACE_PATH, 'P2PDeviceConfig', argument)

    def get_oper_reg_class(self):
        """Returns the OperRegClass"""

        return self.get_p2pdevice_config().get('OperRegClass')

    def set_oper_reg_class(self, oper_reg_class):
        """Sets the OperRegClass"""
        argument = dbus.Dictionary(
            {dbus.String('OperRegClass'):
                dbus.UInt32(oper_reg_class, variant_level=1)},
             signature=dbus.Signature('sv'),
             variant_level=1)

        iface = self._get_dbus_propery_interface()
        iface.Set(self.INTERFACE_PATH, 'P2PDeviceConfig', argument)

    def get_oper_channel(self):
        """Returns the OperChannel"""

        return self.get_p2pdevice_config().get('OperChannel')

    def set_oper_channel(self, oper_channel):
        """Sets the OperChannel"""
        argument = dbus.Dictionary(
            {dbus.String('OperChannel'):
                dbus.UInt32(oper_channel, variant_level=1)},
             signature=dbus.Signature('sv'),
             variant_level=1)

        iface = self._get_dbus_propery_interface()
        iface.Set(self.INTERFACE_PATH, 'P2PDeviceConfig', argument)

    def get_ssid_postfix(self):
        """Returns the SSID postfix"""

        return self.get_p2pdevice_config().get('SsidPostfix')

    def set_ssid_postfix(self, ssid_postfix):
        """Sets the SSID postfix"""
        argument = dbus.Dictionary(
            {dbus.String('SsidPostfix'): dbus.String(ssid_postfix)},
             signature=dbus.Signature('sv'),
             variant_level=1)

        iface = self._get_dbus_propery_interface()
        iface.Set(self.INTERFACE_PATH, 'P2PDeviceConfig', argument)

    def get_intra_bss(self):
        """Returns IntraBss"""

        return self.get_p2pdevice_config().get('IntraBss')

    def set_intra_bss(self, intra_bss):
        """Sets IntraBss"""
        argument = dbus.Dictionary(
            {dbus.String('IntraBss'): dbus.Boolean(intra_bss, variant_level=1)},
             signature=dbus.Signature('sv'),
             variant_level=1)

        iface = self._get_dbus_propery_interface()
        iface.Set(self.INTERFACE_PATH, 'P2PDeviceConfig', argument)

    def get_group_idle(self):
        """Returns GroupIdle"""

        return self.get_p2pdevice_config().get('GroupIdle')

    def set_group_idle(self, group_idle):
        """Sets GroupIdle"""
        argument = dbus.Dictionary(
            {dbus.String('GroupIdle'):
                dbus.UInt32(group_idle, variant_level=1)},
             signature=dbus.Signature('sv'),
             variant_level=1)

        iface = self._get_dbus_propery_interface()
        iface.Set(self.INTERFACE_PATH, 'P2PDeviceConfig', argument)

    def get_disassoc_low_ack(self):
        """Returns disassoc_low_ack"""

        return self.get_p2pdevice_config().get('disassoc_low_ack')

    def set_disassoc_low_ack(self, disassoc_low_ack):
        """Sets disassoc_low_ack"""
        argument = dbus.Dictionary(
            {dbus.String('disassoc_low_ack'):
                dbus.UInt32(disassoc_low_ack, variant_level=1)},
             signature=dbus.Signature('sv'),
             variant_level=1)

        iface = self._get_dbus_propery_interface()
        iface.Set(self.INTERFACE_PATH, 'P2PDeviceConfig', argument)

    def get_no_group_iface(self):
        """Returns NoGroupIface"""

        return self.get_p2pdevice_config().get('NoGroupIface')

    def set_no_group_iface(self, no_group_iface):
        """Sets NoGroupIface"""
        argument = dbus.Dictionary(
            {dbus.String('NoGroupIface'):
                dbus.Boolean(no_group_iface, variant_level=1)},
             signature=dbus.Signature('sv'),
             variant_level=1)

        iface = self._get_dbus_propery_interface()
        iface.Set(self.INTERFACE_PATH, 'P2PDeviceConfig', argument)

    def get_p2p_search_delay(self):
        """Returns p2p_search_delay"""

        return self.get_p2pdevice_config().get('p2p_search_delay')

    def set_p2p_search_delay(self, p2p_search_delay):
        """Sets disassoc_low_ack"""
        argument = dbus.Dictionary(
            {dbus.String('p2p_search_delay'):
                dbus.UInt32(p2p_search_delay, variant_level=1)},
             signature=dbus.Signature('sv'),
             variant_level=1)

        iface = self._get_dbus_propery_interface()
        iface.Set(self.INTERFACE_PATH, 'P2PDeviceConfig', argument)

    def get_peers(self):
        return self.get('Peers')

    def get_role(self):
        return self.get('Role')

    def get_group(self):
        return self.get('Group')

    def get_peer_go(self):
        return self.get('PeerGO')

    def get_persistent_groups(self):
        return self.get('PersistentGroups')


class BSS(BaseIface):
    """Interface implemented by objects representing a scanned BSSs (scan results)"""

    INTERFACE_PATH = 'fi.w1.wpa_supplicant1.BSS'

    iface = DBusInterface(
        INTERFACE_PATH,
        Signal('PropertiesChanged', 'a{sv}')
    )

    def __repr__(self):
        return str(self)

    def __str__(self):
        return "BSS(Path: %s, SSID: %s, BSSID: %s, Signal: %sdBm)" % (self.get_path(),
                                                                      self.get_ssid(),
                                                                      self.get_bssid(),
                                                                      self.get_signal_dbm())

    def to_dict(self):
        """Dict representation of a BSS object"""

        elements = (
            ('ssid', self.get_ssid),
            ('rsn', self.get_rsn),
            ('channel', self.get_channel),
            ('privacy', self.get_privacy),
            ('wpa', self.get_wpa),
            ('signal_dbm', self.get_signal_dbm),
            ('signal_quality', self.get_signal_quality),
            ('network_type', self.get_network_type),
            ('privacy', self.get_privacy),
        )
        out = {}
        for k, v in elements:
            try:
                out[k] = v()
            except:
                logger.exception('Error while fetching BSS information')
        return out

    #
    # Properties
    #
    def get_channel(self):
        """Wi-Fi channel number (1-14)"""
        freq = self.get_frequency()

        if freq == 2484:  # Handle channel 14
            return 14
        elif freq > 2472 or freq < 2412:
            logger.warn('Unexpected frequency %s', freq)
            raise WpaSupplicantException('Unexpected frequency in WiFi connection.')
        else:
            return 1 + (freq - 2412) / 5

    def get_ssid(self):
        """SSID of the BSS in ASCII"""

        return "".join(chr(i) for i in self.get('SSID'))

    def get_bssid(self):
        """BSSID of the BSS as hex bytes delimited by a colon"""

        return ":".join(["{:02X}".format(i) for i in self.get('BSSID')])

    def get_frequency(self):
        """Frequency of the BSS in MHz"""

        return self.get('Frequency')

    def get_wpa(self):
        """WPA information of the BSS, empty dictionary indicates no WPA support

        Dictionaries are::

            {
                "KeyMgmt": <Possible array elements: "wpa-psk", "wpa-eap", "wpa-none">,
                "Pairwise": <Possible array elements: "ccmp", "tkip">,
                "Group": <Possible values are: "ccmp", "tkip", "wep104", "wep40">,
                "MgmtGroup": <Possible values are: "aes128cmac">
            }
        """

        return self.get('WPA')

    def get_rsn(self):
        """RSN/WPA2 information of the BSS, empty dictionary indicates no RSN support

        Dictionaries are::

            {
                "KeyMgmt": <Possible array elements: "wpa-psk", "wpa-eap", "wpa-ft-psk", "wpa-ft-eap", "wpa-psk-sha256", "wpa-eap-sha256">,
                "Pairwise": <Possible array elements: "ccmp", "tkip">,
                "Group": <Possible values are: "ccmp", "tkip", "wep104", "wep40">,
                "MgmtGroup": <Possible values are: "aes128cmac">,
            }
        """

        return self.get('RSN')

    def get_ies(self):
        """All IEs of the BSS as a chain of TLVs"""

        return self.get('IEs')

    def get_privacy(self):
        """Indicates if BSS supports privacy"""

        return self.get('Privacy')

    def get_mode(self):
        """Describes mode of the BSS

            Possible values are: "ad-hoc"
                                 "infrastructure"

        """

        return self.get('Mode')

    def get_rates(self):
        """Descending ordered array of rates supported by the BSS in bits per second"""

        return self.get('Rates')

    def get_signal_dbm(self):
        """Signal strength of the BSS in dBm"""

        return self.get('Signal')

    def get_signal_quality(self):
        """Signal strength of the BSS as a percentage (0-100)"""

        dbm = self.get_signal_dbm()
        if dbm <= -100:
            return 0
        elif dbm >= -50:
            return 100
        else:
            return 2 * (dbm + 100)

    def get_network_type(self):
        """Return the network type as a string

        Possible values are:
                'WPA'
                'WPA2'
                'WEP'
                'OPEN'
        """

        if self.get_privacy():
            rsn_key_mgmt = self.get_rsn().get('KeyMgmt')
            if rsn_key_mgmt:
                return 'WPA2'

            wpa_key_mgmt = self.get_wpa().get('KeyMgmt')
            if wpa_key_mgmt:
                return 'WPA'

            return 'WEP'
        else:
            return 'OPEN'


class Network(BaseIface):
    """Interface implemented by objects representing configured networks"""

    INTERFACE_PATH = 'fi.w1.wpa_supplicant1.Network'

    iface = DBusInterface(
        INTERFACE_PATH,
        Signal('PropertiesChanged', 'a{sv}')
    )

    def __repr__(self):
        return str(self)

    def __str__(self):
        return "Network(Path: %s, Properties: %s)" % (self.get_path(),
                                                      self.get_properties())

    #
    # Properties
    #
    def get_properties(self):
        """Properties of the configured network

        Dictionary contains entries from "network" block of  wpa_supplicant.conf.
        All values are string type, e.g., frequency is "2437", not 2437.
        """

        network_properties = self.get('Properties')
        ssid = network_properties.get('ssid', '')
        if ssid:
            quote_chars = {'"', "'"}
            ssid = ssid[1:] if ssid[0] in quote_chars else ssid
            ssid = ssid[:-1] if ssid[-1] in quote_chars else ssid
        network_properties.update({
            'ssid': ssid
        })
        return network_properties

    def get_enabled(self):
        """Determines if the configured network is enabled or not"""

        return self.get('Enabled')


class WpaSupplicantDriver(object):
    """Driver object for starting, stopping and connecting to wpa_supplicant"""

    def __init__(self, reactor):
        self._reactor = reactor

    @_catch_remote_errors
    def connect(self):
        """Connect to wpa_supplicant over D-Bus

        :returns: Remote D-Bus proxy object of the root wpa_supplicant interface
        :rtype: :class:`~WpaSupplicant`
        """

        if not self._reactor.running:
            raise ReactorNotRunning('Twisted Reactor must be started (call .run())')

        @defer.inlineCallbacks
        def get_conn():
            self._reactor.thread_name = threading.currentThread().getName()
            conn = yield client.connect(self._reactor, busAddress='system')
            defer.returnValue(conn)

        conn = threads.blockingCallFromThread(self._reactor, get_conn)
        return WpaSupplicant('/fi/w1/wpa_supplicant1', conn, self._reactor, )
