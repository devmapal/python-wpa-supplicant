# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Copyright (c) 2015 Digi International Inc. All Rights Reserved.

"""This module provides classes for mocking out the txdbus layer at a low level

Mocking out things at this layer means that some of the txdbus code is being tested
in addition to the wpa_supplicant library code.  Although a-typical of traditional unit
testing this approach proved beneficial in practice.
"""

from twisted.internet import defer
from collections import OrderedDict
from txdbus import client, error
import mock
import re


def init():
    """Call at the beginning of ``testCase.setUp`` to mock D-Bus Layer"""

    conn = MockConnection()

    @defer.inlineCallbacks
    def mock_connect(reactor, busAddress):
        yield
        defer.returnValue(conn)

    client.connect = mock_connect


class MockDBusObject(object):
    """Base class for mocked txdbus objects which implement D-Bus interfaces"""

    def __init__(self, *args, **kwargs):
        self.callRemote = mock.Mock(side_effect=self._callRemote)
        self.notifyOnSignal = mock.Mock(side_effect=self._notifyOnSignal)
        self.cancelSignalNotification = mock.Mock(
            side_effect=self._cancelSignalNotification)

        self._signals = dict()

    def _callRemote(self, method_name, *args):
        if method_name == 'Get':
            property_name = args[1]
            property_method = getattr(self, 'Get_%s' % property_name)
            if property_method:
                deferred = mock.Mock()
                deferred.result = property_method()
                deferred.called = True
                return deferred
            else:
                return None

        elif method_name == 'Set':
            return NotImplementedError()

        else:
            native_method = getattr(self, method_name)
            if native_method:
                deferred = mock.Mock()
                deferred.result = native_method(*args)
                deferred.called = True
                return deferred

    def _notifyOnSignal(self, signal_name, callback, interface=None):
        d = defer.Deferred()
        d.addCallback(callback)
        self._signals.setdefault(signal_name, list()).append(d)
        return d

    def _cancelSignalNotification(self, rule_id):
        pass

    def fire_signal(self, signal_name, value):
        deferreds = self._signals.get(signal_name, None)
        if deferreds is not None:
            for d in deferreds:
                d.callback(value)


class MockWpaSupplicant(MockDBusObject):
    """Mock wpa_supplicant `Root` object"""

    def __init__(self, *args, **kwargs):
        MockDBusObject.__init__(self, *args, **kwargs)
        self._valid_interfaces = {
            'wlan0': '/fi/w1/wpa_supplicant1/Interfaces/3',
            'sta0': '/fi/w1/wpa_supplicant1/Interfaces/4'
        }
        self._created_interfaces = []

    #
    # Methods
    #
    def GetInterface(self, interface_name):
        if interface_name in self._valid_interfaces:
            return self._valid_interfaces.get(interface_name)
        else:
            raise error.RemoteError('fi.w1.wpa_supplicant1.InterfaceUnknown')

    def RemoveInterface(self, interface_path):
        for i, path in enumerate(self._created_interfaces[:]):
            if path == interface_path:
                del self._created_interfaces[i]
                break
        else:
            raise error.RemoteError('fi.w1.wpa_supplicant1.InterfaceUnknown')

    def CreateInterface(self, cfg):
        if not isinstance(cfg, dict):
            raise error.RemoteError('fi.w1.wpa_supplicant1.InvalidArgs')

        interface_name = cfg.get('Ifname', None)  # required argument
        if interface_name is None:
            raise error.RemoteError('fi.w1.wpa_supplicant1.InvalidArgs')

        iface_path = self._valid_interfaces.get(interface_name)

        if not iface_path:
            raise error.RemoteError('fi.w1.wpa_supplicant1.UnknownError')

        if iface_path in self._created_interfaces:
            raise error.RemoteError('fi.w1.wpa_supplicant1.InterfaceExists')

        self._created_interfaces.append(iface_path)
        return iface_path

    #
    # Properties
    #
    def Get_Interfaces(self):
        return [u'/fi/w1/wpa_supplicant1/Interfaces/7']

    def Get_EapMethods(self):
        return [u'MD5',
                u'TLS',
                u'MSCHAPV2',
                u'PEAP',
                u'TTLS',
                u'GTC',
                u'OTP',
                u'SIM',
                u'LEAP',
                u'PSK',
                u'AKA',
                u"AKA'",
                u'FAST',
                u'PAX',
                u'SAKE',
                u'GPSK',
                u'WSC',
                u'IKEV2',
                u'TNC',
                u'PWD']

    def Get_DebugLevel(self):
        return u'info'

    def Get_DebugShowKeys(self):
        return False

    def Get_DebugTimestamp(self):
        return False


class MockInterfaceObject(MockDBusObject):
    """Mock wpa_supplicant `Interface` object"""

    def __init__(self, *args, **kwargs):
        MockDBusObject.__init__(self, *args, **kwargs)

        # Internal State
        self._network_counter = -1
        self._networks = dict()
        self._current_network = None

    #
    # Methods
    #
    def Scan(self, scan_config):
        return None

    def AddNetwork(self, cfg):
        self._network_counter += 1
        network_path = '/fi/w1/wpa_supplicant1/Networks/%s' % self._network_counter
        self._networks[network_path] = cfg
        return network_path

    def RemoveNetwork(self, network_path):
        if network_path in self._networks:
            del self._networks[network_path]
        else:
            raise error.RemoteError('fi.w1.wpa_supplicant1.NetworkUnknown')

    def SelectNetwork(self, network_path):
        self._current_network = network_path

    def Disconnect(self):
        if self._current_network is None:
            raise error.RemoteError('fi.w1.wpa_supplicant1.NotConnected')
        else:
            self._current_network = None

    #
    # Properties
    #
    def Get_Networks(self):
        return []

    def Get_FastReauth(self):
        return True

    def Get_ScanInterval(self):
        return 5

    def Get_CurrentNetwork(self):
        return self._current_network

    def Get_Ifname(self):
        return u'wlan0'

    def Get_BSSs(self):
        return ['/fi/w1/wpa_supplicant1/Interfaces/3/BSSs/1234', ]

    def Get_CurrentBSS(self):
        return '/fi/w1/wpa_supplicant1/Interfaces/3/BSSs/1234'

    def Get_ApScan(self):
        return 1

    def Get_Scanning(self):
        return False

    def Get_State(self):
        return u'inactive'

    def Get_Capabilities(self):
        return {u'AuthAlg': [u'open', u'shared', u'leap'],
                u'Group': [u'ccmp', u'tkip', u'wep104', u'wep40'],
                u'KeyMgmt': [u'none',
                             u'ieee8021x',
                             u'wpa-eap',
                             u'wpa-ft-eap',
                             u'wpa-eap-sha256',
                             u'wpa-psk',
                             u'wpa-ft-psk',
                             u'wpa-psk-sha256',
                             u'wps'],
                u'MaxScanSSID': 4,
                u'Modes': [u'infrastructure', u'ad-hoc', u'ap'],
                u'Pairwise': [u'ccmp', u'tkip'],
                u'Protocol': [u'rsn', u'wpa'],
                u'Scan': [u'active', u'passive', u'ssid']}

    def Get_Country(self):
        return u'US'

    def Get_BSSExpireAge(self):
        return 180

    def Get_BSSExpireCount(self):
        return 2

    def Get_BridgeIfname(self):
        return u''

    def Get_Driver(self):
        return u'nl80211'


class MockBSSObject(MockDBusObject):
    """Mock txdbus/wpa_supplicant .fi.w1.wpa_supplicant1.BSS object"""

    #
    # Properties
    #
    def Get_BSSID(self):
        # This should return an array of bytes, for testing it is ideal
        # if the bytes fall within the ASCII range
        return [70, 71, 72, 73]

    def Get_SSID(self):
        # This should return an array of bytes, for testing it is ideal
        # if the bytes fall within the ASCII range
        return [70, 71, 72, 73]

    def Get_WPA(self):
        return {u'Group': u'tkip', u'KeyMgmt': [u'wpa-psk'], u'Pairwise': [u'tkip']}

    def Get_RSN(self):
        return {u'Group': u'', u'KeyMgmt': [], u'Pairwise': []}

    def Get_IEs(self):
        return [0,
                9,
                68,
                65,
                80,
                150,
                24,
                36]

    def Get_Privacy(self):
        return True

    def Get_Mode(self):
        return u'infrastructure'

    def Get_Frequency(self):
        return 2462

    def Get_Rates(self):
        return [54000000, 48000000, 6000000]

    def Get_Signal(self):
        return -60


class MockNetworkObject(MockDBusObject):
    """Mock txdbus/wpa_supplicant .fi.w1.wpa_supplicant1.Networks object"""

    def Get_Properties(self):
        return {
            u'ap_max_inactivity': u'0',
            u'beacon_int': u'0',
            u'bg_scan_period': u'-1',
            u'disabled': u'0',
            u'dtim_period': u'0',
            u'eap_workaround': u'-1',
            u'eapol_flags': u'3',
            u'engine': u'0',
            u'engine2': u'0',
            u'fragment_size': u'1398',
            u'frequency': u'0',
            u'group': u'CCMP TKIP WEP104 WEP40',
            u'ignore_broadcast_ssid': u'0',
            u'key_mgmt': u'WPA-PSK WPA-EAP',
            u'mixed_cell': u'0',
            u'mode': u'0',
            u'ocsp': u'0',
            u'pairwise': u'CCMP TKIP',
            u'peerkey': u'0',
            u'priority': u'0',
            u'proactive_key_caching': u'-1',
            u'proto': u'WPA RSN',
            u'scan_ssid': u'0',
            u'ssid': u'"wdnu-dvt1"',
            u'wep_tx_keyidx': u'0',
            u'wpa_ptk_rekey': u'0'
        }


class MockP2PDeviceObject(object):
    """Mock txdbus/wpa_supplicant .fi.w1.wpa_supplicant1.P2PDevice object"""

    #
    # Methods
    #
    def Find(self, cfg):
        return None

    def StopFind(self):
        return None

    def Listen(self, timeout):
        return None

    def ExtendedListen(self, cfg):
        return None

    def PresenceRequest(self, cfg):
        return None

    def ProvisionDiscoveryRequest(self, cfg):
        return None

    def Connect(self, cfg):
        frequency = cfg.get('frequency')
        if frequency is not None and frequency < 0:
            raise error.RemoteError(
                'Did not receive correct message arguments.')

        go_intent = cfg.get('go_intent')
        if go_intent < 0 or go_intent > 15:
            raise error.RemoteError(
                'Did not receive correct message arguments.')

        wps_method = cfg.get('wps_method')
        if wps_method not in ('pbc', 'display', 'keypad', 'pin'):
            raise error.RemoteError(
                'Did not receive correct message arguments.')

        pin = cfg.get('pin')
        if wps_method == 'keypad' and pin is None:
            raise error.RemoteError(
                'Did not receive correct message arguments.')

        return u'123456'

    def GroupAdd(self, cfg):
        frequency = cfg.get('frequency')
        if frequency is not None and frequency < 0:
            raise error.RemoteError(
                'Did not receive correct message arguments.')

    def Cancel(self):
        return None

    def Invite(self, cfg):
        return None

    def Disconnect(self):
        return None
    
    def RejectPeer(self, peer):
        return None

    def RemoveClient(self, cfg):
        return None

    def Flush(self):
        return None
    
    def AddService(self, cfg):
        service_type = cfg.get('service_type')
        version = cfg.get('version')
        if service_type == 'upnp' and version is None:
            raise error.RemoteError(
                'Did not receive correct message arguments.')
    
    def DeleteService(self, cfg):
        service_type = cfg.get('service_type')
        version = cfg.get('version')
        if service_type == 'upnp' and version is None:
            raise error.RemoteError(
                'Did not receive correct message arguments.')

    def FlushService(self):
        return None

    def ServiceDiscoveryRequest(self, cfg):
        service_type = cfg.get('service_type')
        version = cfg.get('version')
        if(service_type is not None and
           service_type == 'upnp' and
           version is None):
            raise error.RemoteError(
                'Did not receive correct message arguments.')

        return 12

    def ServiceDiscoveryResponse(self, cfg):
        return None

    def ServiceDiscoveryCancelRequest(self, token):
        return None

    def ServiceUpdate(self):
        return None

    def ServiceDiscoveryExternal(self, i):
        return None

    def AddPersistentGroup(self, cfg):
        return u'/'

    def RemovePersistentGroup(self, path):
        return None

    def RemoveAllPersistentGroups(self):
        return None
    


    #
    # Properties
    #
    def Get_P2PDeviceConfig(self):
        return {
            u'DeviceName': u'a device',
            u'PrimaryDeviceType': [1, 2, 3, 4, 5, 6, 7, 8],
            u'SecondaryDeviceTypes': [[1, 2, 3, 4, 5, 6, 7, 8],
                                      [8, 7, 6, 5, 4, 3, 2, 1]],
            u'VendorExtension': [[3, 2, 3, 4, 5, 6, 7, 8],
                                 [8, 7, 4, 5, 4, 3, 2, 1]],
            u'GOIntent': 3,
            u'PersistentReconnect': True,
            u'ListenRegClass': 543,
            u'ListenChannel': 43,
            u'OperRegClass': 22,
            u'OperChannel': 555,
            u'SsidPostfix': u'postfix',
            u'IntraBss': True,
            u'GroupIdle': 666,
            u'disassoc_low_ack': 1,
            u'NoGroupIface': True,
            u'p2p_search_delay': u'500'
        }

        def Get_Peers(self):
            return [u'/fi/w1/wpa_supplicant1/Interfaces/1/Peers/32f77203e5a5',
                     u'/fi/w1/wpa_supplicant1/Interfaces/1/Peers/02044b2906fb']

        def Get_Role(self):
            return u'device'

        def Get_Group(self):
            return u'/'

        def Get_PeerGO(self):
            return u'/'


class MockConnection(object):
    """Mock txdbus client connection to a D-Bus server"""

    mock_objects = OrderedDict([
        ('/fi/w1/wpa_supplicant1/Interfaces/.+/BSSs/.+', MockBSSObject),
        ('/fi/w1/wpa_supplicant1/Networks/.+', MockNetworkObject),
        # TODO: Need to differentiate also on interface path
        ('/fi/w1/wpa_supplicant1/Interfaces/.+', MockP2PDeviceObject),
        ('/fi/w1/wpa_supplicant1/Interfaces/.+', MockInterfaceObject),
        ('/fi/w1/wpa_supplicant1', MockWpaSupplicant)
    ])

    @defer.inlineCallbacks
    def getRemoteObject(self, busName, objectPath, interfaces=None):
        print busName
        print objectPath
        print interfaces
        interface_object = None
        for opath, interface in self.mock_objects.items():
            match = re.match(opath, objectPath)
            if match:
                interface_object = interface()
                break
        yield
        defer.returnValue(interface_object)
