# -*- coding: utf-8 -*-
#
# Project Kimchi
#
# Copyright IBM Corp, 2013-2016
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA

import grp
import os
import pwd
import re
import shutil
import time
import unittest

import tests.utils as utils

import wok.objectstore
from wok.basemodel import Singleton
from wok.config import config
from wok.exception import InvalidOperation
from wok.exception import InvalidParameter, NotFoundError, OperationFailed
from wok.rollbackcontext import RollbackContext
from wok.utils import add_task, get_task_id
from wok.xmlutils.utils import xpath_get_text

from wok.plugins.kimchi import netinfo
from wok.plugins.kimchi import osinfo
from wok.plugins.kimchi.config import kimchiPaths as paths
from wok.plugins.kimchi.model import model
from wok.plugins.kimchi.model.libvirtconnection import LibvirtConnection
from wok.plugins.kimchi.model.vms import VMModel

import iso_gen


invalid_repository_urls = ['www.fedora.org',       # missing protocol
                           '://www.fedora.org',    # missing protocol
                           'http://www.fedora',    # invalid domain name
                           'file:///home/foobar']  # invalid path

TMP_DIR = '/var/lib/kimchi/tests/'
UBUNTU_ISO = TMP_DIR + 'ubuntu14.04.iso'


def setUpModule():
    if not os.path.exists(TMP_DIR):
        os.makedirs(TMP_DIR)

    iso_gen.construct_fake_iso(UBUNTU_ISO, True, '14.04', 'ubuntu')

    # Some FeatureTests functions depend on server to validate their result.
    # As CapabilitiesModel is a Singleton class it will get the first result
    # from FeatureTests which may be wrong when using the Model instance
    # directly - the case of this test_model.py
    # So clean Singleton instances to make sure to get the right result when
    # running the following tests.
    Singleton._instances = {}


def tearDownModule():
    shutil.rmtree(TMP_DIR)


def _setDiskPoolDefault():
    osinfo.defaults['disks'][0]['pool'] = {
        'name': '/plugins/kimchi/storagepools/default'}


def _setDiskPoolDefaultTest():
    osinfo.defaults['disks'][0]['pool'] = {
        'name': '/plugins/kimchi/storagepools/default-pool'}


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp_store = '/tmp/kimchi-store-test'

    def tearDown(self):
        # FIXME: Tests using 'test:///default' URI should be moved to
        # test_rest or test_mockmodel to avoid overriding problems
        LibvirtConnection._connections['test:///default'] = {}

        os.unlink(self.tmp_store)

    def test_vm_info(self):
        inst = model.Model('test:///default', self.tmp_store)
        vms = inst.vms_get_list()
        self.assertEquals(1, len(vms))
        self.assertEquals('test', vms[0])

        keys = set(('name', 'state', 'stats', 'uuid', 'memory', 'cpu_info',
                    'screenshot', 'icon', 'graphics', 'users', 'groups',
                    'access', 'persistent'))

        stats_keys = set(('cpu_utilization', 'mem_utilization',
                          'net_throughput', 'net_throughput_peak',
                          'io_throughput', 'io_throughput_peak'))
        info = inst.vm_lookup('test')
        self.assertEquals(keys, set(info.keys()))
        self.assertEquals('running', info['state'])
        self.assertEquals('test', info['name'])
        self.assertEquals(2048, info['memory']['current'])
        self.assertEquals(2, info['cpu_info']['vcpus'])
        self.assertEquals(2, info['cpu_info']['maxvcpus'])
        self.assertEquals(None, info['icon'])
        self.assertEquals(stats_keys, set(info['stats'].keys()))
        self.assertRaises(NotFoundError, inst.vm_lookup, 'nosuchvm')
        self.assertEquals([], info['users'])
        self.assertEquals([], info['groups'])
        self.assertTrue(info['persistent'])

    @unittest.skipUnless(utils.running_as_root(), 'Must be run as root')
    def test_vm_lifecycle(self):
        inst = model.Model(objstore_loc=self.tmp_store)

        with RollbackContext() as rollback:
            vol_params = {'name': u'test-vol', 'capacity': 1024}
            task = inst.storagevolumes_create(u'default', vol_params)
            rollback.prependDefer(inst.storagevolume_delete, u'default',
                                  vol_params['name'])
            inst.task_wait(task['id'])
            task = inst.task_lookup(task['id'])
            self.assertEquals('finished', task['status'])

            params = {'name': 'test', 'source_media': UBUNTU_ISO}

            inst.templates_create(params)
            rollback.prependDefer(inst.template_delete, 'test')

            params = {'name': 'kimchi-vm',
                      'template': '/plugins/kimchi/templates/test'}
            task = inst.vms_create(params)
            rollback.prependDefer(inst.vm_delete, 'kimchi-vm')
            inst.task_wait(task['id'], 10)
            task = inst.task_lookup(task['id'])
            self.assertEquals('finished', task['status'])

            vms = inst.vms_get_list()
            self.assertTrue('kimchi-vm' in vms)

            inst.vm_start('kimchi-vm')

            info = inst.vm_lookup('kimchi-vm')
            self.assertEquals('running', info['state'])

            self.assertRaises(InvalidOperation, inst.vmsnapshots_create,
                              u'kimchi-vm')

            inst.vm_poweroff(u'kimchi-vm')
            vm = inst.vm_lookup(u'kimchi-vm')

            empty_snap = inst.currentvmsnapshot_lookup(u'kimchi-vm')
            self.assertEquals({}, empty_snap)

            # this snapshot should be deleted when its VM is deleted
            params = {'name': u'mysnap'}
            task = inst.vmsnapshots_create(u'kimchi-vm', params)
            inst.task_wait(task['id'])
            task = inst.task_lookup(task['id'])
            self.assertEquals('finished', task['status'])

            self.assertRaises(NotFoundError, inst.vmsnapshot_lookup,
                              u'kimchi-vm', u'foobar')

            snap = inst.vmsnapshot_lookup(u'kimchi-vm', params['name'])
            self.assertTrue(int(time.time()) >= int(snap['created']))
            self.assertEquals(vm['state'], snap['state'])
            self.assertEquals(params['name'], snap['name'])
            self.assertEquals(u'', snap['parent'])

            snaps = inst.vmsnapshots_get_list(u'kimchi-vm')
            self.assertEquals([params['name']], snaps)

            current_snap = inst.currentvmsnapshot_lookup(u'kimchi-vm')
            self.assertEquals(snap, current_snap)

            task = inst.vmsnapshots_create(u'kimchi-vm')
            snap_name = task['target_uri'].split('/')[-1]
            rollback.prependDefer(inst.vmsnapshot_delete,
                                  u'kimchi-vm', snap_name)
            inst.task_wait(task['id'])
            task = inst.task_lookup(task['id'])
            self.assertEquals('finished', task['status'])

            snaps = inst.vmsnapshots_get_list(u'kimchi-vm')
            self.assertEquals(sorted([params['name'], snap_name],
                              key=unicode.lower), snaps)

            snap = inst.vmsnapshot_lookup(u'kimchi-vm', snap_name)
            current_snap = inst.currentvmsnapshot_lookup(u'kimchi-vm')
            self.assertEquals(snap, current_snap)

            # update vm name
            inst.vm_update('kimchi-vm', {'name': u'kimchi-vm-new'})

            # Look up the first created snapshot from the renamed vm
            snap = inst.vmsnapshot_lookup(u'kimchi-vm-new', params['name'])

            # snapshot revert to the first created vm
            result = inst.vmsnapshot_revert(u'kimchi-vm-new', params['name'])
            self.assertEquals(result, [u'kimchi-vm', snap['name']])

            vm = inst.vm_lookup(u'kimchi-vm')
            self.assertEquals(vm['state'], snap['state'])

            current_snap = inst.currentvmsnapshot_lookup(u'kimchi-vm')
            self.assertEquals(params['name'], current_snap['name'])

            self.assertRaises(NotFoundError, inst.vmsnapshot_delete,
                              u'kimchi-vm', u'foobar')

            # suspend and resume the VM
            info = inst.vm_lookup(u'kimchi-vm')
            self.assertEquals(info['state'], 'shutoff')
            self.assertRaises(InvalidOperation, inst.vm_suspend, u'kimchi-vm')
            inst.vm_start(u'kimchi-vm')
            info = inst.vm_lookup(u'kimchi-vm')
            self.assertEquals(info['state'], 'running')
            inst.vm_suspend(u'kimchi-vm')
            info = inst.vm_lookup(u'kimchi-vm')
            self.assertEquals(info['state'], 'paused')
            self.assertRaises(InvalidParameter, inst.vm_update, u'kimchi-vm',
                              {'name': 'foo'})
            inst.vm_resume(u'kimchi-vm')
            info = inst.vm_lookup(u'kimchi-vm')
            self.assertEquals(info['state'], 'running')
            self.assertRaises(InvalidOperation, inst.vm_resume, u'kimchi-vm')
            # leave the VM suspended to make sure a paused VM can be
            # deleted correctly
            inst.vm_suspend(u'kimchi-vm')

        vms = inst.vms_get_list()
        self.assertFalse('kimchi-vm' in vms)

    @unittest.skipUnless(utils.running_as_root(), 'Must be run as root')
    def test_image_based_template(self):
        inst = model.Model(objstore_loc=self.tmp_store)

        with RollbackContext() as rollback:
            vol = 'base-vol.img'
            params = {'name': vol,
                      'capacity': 1073741824,  # 1 GiB
                      'allocation': 1048576,  # 1 MiB
                      'format': 'qcow2'}
            task_id = inst.storagevolumes_create('default', params)['id']
            rollback.prependDefer(inst.storagevolume_delete, 'default', vol)
            inst.task_wait(task_id)
            self.assertEquals('finished', inst.task_lookup(task_id)['status'])
            vol_path = inst.storagevolume_lookup('default', vol)['path']

            # Create template based on IMG file
            tmpl_name = "img-tmpl"
            tmpl_info = {"cpu_info": {"vcpus": 1}, "name": tmpl_name,
                         "graphics": {"type": "vnc", "listen": "127.0.0.1"},
                         "networks": ["default"], "memory": {'current': 1024},
                         "folder": [], "icon": "images/icon-vm.png",
                         "os_distro": "unknown", "os_version": "unknown",
                         "source_media": vol_path}

            inst.templates_create(tmpl_info)
            rollback.prependDefer(inst.template_delete, tmpl_name)

            # verify disk
            tmpl = inst.template_lookup(tmpl_name)
            self.assertEquals(vol_path, tmpl["disks"][0]["base"])

            params = {'name': 'kimchi-vm',
                      'template': '/plugins/kimchi/templates/img-tmpl'}
            task = inst.vms_create(params)
            inst.task_wait(task['id'])
            rollback.prependDefer(inst.vm_delete, 'kimchi-vm')

            vms = inst.vms_get_list()
            self.assertTrue('kimchi-vm' in vms)

            inst.vm_start('kimchi-vm')
            rollback.prependDefer(inst.vm_poweroff, 'kimchi-vm')

            info = inst.vm_lookup('kimchi-vm')
            self.assertEquals('running', info['state'])

    @unittest.skipUnless(utils.running_as_root(), 'Must be run as root')
    def test_vm_graphics(self):
        inst = model.Model(objstore_loc=self.tmp_store)
        params = {'name': 'test', 'source_media': UBUNTU_ISO}
        inst.templates_create(params)
        with RollbackContext() as rollback:
            params = {'name': 'kimchi-vnc',
                      'template': '/plugins/kimchi/templates/test'}
            task1 = inst.vms_create(params)
            inst.task_wait(task1['id'])
            rollback.prependDefer(inst.vm_delete, 'kimchi-vnc')

            info = inst.vm_lookup('kimchi-vnc')
            self.assertEquals('vnc', info['graphics']['type'])
            self.assertEquals('127.0.0.1', info['graphics']['listen'])

            graphics = {'type': 'spice', 'listen': '127.0.0.1'}
            params = {'name': 'kimchi-spice',
                      'template': '/plugins/kimchi/templates/test',
                      'graphics': graphics}
            task2 = inst.vms_create(params)
            inst.task_wait(task2['id'])
            rollback.prependDefer(inst.vm_delete, 'kimchi-spice')

            info = inst.vm_lookup('kimchi-spice')
            self.assertEquals('spice', info['graphics']['type'])
            self.assertEquals('127.0.0.1', info['graphics']['listen'])

        inst.template_delete('test')

    @unittest.skipUnless(utils.running_as_root(), "Must be run as root")
    def test_vm_serial(self):
        inst = model.Model(objstore_loc=self.tmp_store)
        params = {'name': 'test', 'source_media': UBUNTU_ISO}
        inst.templates_create(params)
        with RollbackContext() as rollback:
            params = {'name': 'kimchi-serial',
                      'template': '/plugins/kimchi/templates/test'}
            task1 = inst.vms_create(params)
            inst.task_wait(task1['id'])
            rollback.prependDefer(inst.vm_delete, 'kimchi-serial')

            inst.vm_start('kimchi-serial')
            rollback.prependDefer(inst.vm_poweroff, 'kimchi-serial')

            with self.assertRaises(OperationFailed):
                inst.vm_serial('kimchi-serial')

        inst.template_delete('test')

    @unittest.skipUnless(utils.running_as_root(), 'Must be run as root')
    def test_vm_ifaces(self):
        inst = model.Model(objstore_loc=self.tmp_store)
        with RollbackContext() as rollback:
            params = {'name': 'test', 'source_media': UBUNTU_ISO}
            inst.templates_create(params)
            rollback.prependDefer(inst.template_delete, 'test')

            # Create a network
            net_name = 'test-network'
            net_args = {'name': net_name,
                        'connection': 'nat',
                        'subnet': '127.0.100.0/24'}
            inst.networks_create(net_args)
            rollback.prependDefer(inst.network_delete, net_name)
            inst.network_activate(net_name)
            rollback.prependDefer(inst.network_deactivate, net_name)

            for vm_name in ['kimchi-ifaces', 'kimchi-ifaces-running']:
                params = {'name': vm_name,
                          'template': '/plugins/kimchi/templates/test'}
                task = inst.vms_create(params)
                inst.task_wait(task['id'])
                rollback.prependDefer(inst.vm_delete, vm_name)

                ifaces = inst.vmifaces_get_list(vm_name)
                self.assertEquals(1, len(ifaces))

                iface = inst.vmiface_lookup(vm_name, ifaces[0])
                self.assertEquals(17, len(iface['mac']))
                self.assertEquals("default", iface['network'])
                self.assertIn("model", iface)

                # attach network interface to vm
                iface_args = {"type": "network",
                              "network": "test-network",
                              "model": "virtio"}
                mac = inst.vmifaces_create(vm_name, iface_args)
                # detach network interface from vm
                rollback.prependDefer(inst.vmiface_delete, vm_name, mac)
                self.assertEquals(17, len(mac))

                iface = inst.vmiface_lookup(vm_name, mac)
                self.assertEquals("network", iface["type"])
                self.assertEquals("test-network", iface['network'])
                self.assertEquals("virtio", iface["model"])

                # attach network interface to vm without providing model
                iface_args = {"type": "network",
                              "network": "test-network"}
                mac = inst.vmifaces_create(vm_name, iface_args)
                rollback.prependDefer(inst.vmiface_delete, vm_name, mac)

                iface = inst.vmiface_lookup(vm_name, mac)
                self.assertEquals("network", iface["type"])
                self.assertEquals("test-network", iface['network'])

                # update vm interface
                newMacAddr = '54:50:e3:44:8a:af'
                iface_args = {"mac": newMacAddr}
                inst.vmiface_update(vm_name, mac, iface_args)
                iface = inst.vmiface_lookup(vm_name, newMacAddr)
                self.assertEquals(newMacAddr, iface['mac'])

                # undo mac address change
                iface_args = {"mac": mac}
                inst.vmiface_update(vm_name, newMacAddr, iface_args)
                iface = inst.vmiface_lookup(vm_name, mac)
                self.assertEquals(mac, iface['mac'])

    @unittest.skipUnless(utils.running_as_root(), 'Must be run as root')
    def test_vm_disk(self):
        disk_path = os.path.join(TMP_DIR, 'existent2.iso')
        open(disk_path, 'w').close()
        modern_disk_bus = osinfo.get_template_default('modern', 'disk_bus')

        def _attach_disk(expect_bus=modern_disk_bus):
            disk_args = {"type": "disk",
                         "pool": pool,
                         "vol": vol}
            disk = inst.vmstorages_create(vm_name, disk_args)
            storage_list = inst.vmstorages_get_list(vm_name)
            self.assertEquals(prev_count + 1, len(storage_list))

            # Check the bus type to be 'virtio'
            disk_info = inst.vmstorage_lookup(vm_name, disk)
            self.assertEquals(u'disk', disk_info['type'])
            self.assertEquals(vol_path, disk_info['path'])
            self.assertEquals(expect_bus, disk_info['bus'])
            return disk

        inst = model.Model(objstore_loc=self.tmp_store)
        with RollbackContext() as rollback:
            path = os.path.join(TMP_DIR, 'kimchi-images')
            pool = 'test-pool'
            vol = 'test-volume.img'
            vol_path = "%s/%s" % (path, vol)
            if not os.path.exists(path):
                os.mkdir(path)
            rollback.prependDefer(shutil.rmtree, path)

            args = {'name': pool,
                    'path': path,
                    'type': 'dir'}
            inst.storagepools_create(args)
            rollback.prependDefer(inst.storagepool_delete, pool)

            # Activate the pool before adding any volume
            inst.storagepool_activate(pool)
            rollback.prependDefer(inst.storagepool_deactivate, pool)

            params = {'name': vol,
                      'capacity': 1073741824,  # 1 GiB
                      'allocation': 536870912,  # 512 MiB
                      'format': 'qcow2'}
            task_id = inst.storagevolumes_create(pool, params)['id']
            rollback.prependDefer(inst.storagevolume_delete, pool, vol)
            inst.task_wait(task_id)

            vm_name = 'kimchi-cdrom'
            params = {'name': 'test', 'disks': [], 'source_media': UBUNTU_ISO}
            inst.templates_create(params)
            rollback.prependDefer(inst.template_delete, 'test')
            params = {'name': vm_name,
                      'template': '/plugins/kimchi/templates/test'}
            task1 = inst.vms_create(params)
            inst.task_wait(task1['id'])
            rollback.prependDefer(inst.vm_delete, vm_name)

            prev_count = len(inst.vmstorages_get_list(vm_name))
            self.assertEquals(1, prev_count)

            # Volume format with mismatched type raise error
            cdrom_args = {"type": "cdrom", "pool": pool, "vol": vol}
            self.assertRaises(InvalidParameter, inst.vmstorages_create,
                              vm_name, cdrom_args)

            # Cold plug and unplug a disk
            disk = _attach_disk()
            inst.vmstorage_delete(vm_name, disk)

            # Hot plug a disk
            inst.vm_start(vm_name)
            disk = _attach_disk()

            # VM disk still there after powered off
            inst.vm_poweroff(vm_name)
            disk_info = inst.vmstorage_lookup(vm_name, disk)
            self.assertEquals(u'disk', disk_info['type'])
            inst.vmstorage_delete(vm_name, disk)

            # Specifying pool and path at same time will fail
            disk_args = {"type": "disk",
                         "pool": pool,
                         "vol": vol,
                         "path": disk_path}
            self.assertRaises(
                InvalidParameter, inst.vmstorages_create, vm_name, disk_args)

            old_distro_iso = TMP_DIR + 'rhel4_8.iso'
            iso_gen.construct_fake_iso(old_distro_iso, True, '4.8', 'rhel')

            vm_name = 'kimchi-ide-bus-vm'
            params = {'name': 'old_distro_template', 'disks': [],
                      'source_media': old_distro_iso}
            inst.templates_create(params)
            rollback.prependDefer(inst.template_delete, 'old_distro_template')
            params = {
                'name': vm_name,
                'template': '/plugins/kimchi/templates/old_distro_template'
            }
            task2 = inst.vms_create(params)
            inst.task_wait(task2['id'])
            rollback.prependDefer(inst.vm_delete, vm_name)

            # Need to check the right disk_bus for old distro
            disk = _attach_disk(osinfo.get_template_default('old', 'disk_bus'))
            inst.vmstorage_delete('kimchi-ide-bus-vm', disk)

            # Hot plug IDE bus disk does not work
            inst.vm_start(vm_name)
            self.assertRaises(InvalidOperation, _attach_disk)
            inst.vm_poweroff(vm_name)

    @unittest.skipUnless(utils.running_as_root(), 'Must be run as root')
    def test_vm_cdrom(self):
        inst = model.Model(objstore_loc=self.tmp_store)
        with RollbackContext() as rollback:
            vm_name = 'kimchi-cdrom'
            params = {'name': 'test', 'disks': [], 'source_media': UBUNTU_ISO}
            inst.templates_create(params)
            rollback.prependDefer(inst.template_delete, 'test')
            params = {'name': vm_name,
                      'template': '/plugins/kimchi/templates/test'}
            task = inst.vms_create(params)
            inst.task_wait(task['id'])
            rollback.prependDefer(inst.vm_delete, vm_name)

            prev_count = len(inst.vmstorages_get_list(vm_name))
            self.assertEquals(1, prev_count)

            # dummy .iso files
            iso_path = os.path.join(TMP_DIR, 'existent.iso')
            iso_path2 = os.path.join(TMP_DIR, 'existent2.iso')
            open(iso_path, 'w').close()
            rollback.prependDefer(os.remove, iso_path)
            open(iso_path2, 'w').close()
            rollback.prependDefer(os.remove, iso_path2)
            wrong_iso_path = '/nonexistent.iso'

            # Create a cdrom
            cdrom_args = {"type": "cdrom",
                          "path": iso_path}
            cdrom_dev = inst.vmstorages_create(vm_name, cdrom_args)
            storage_list = inst.vmstorages_get_list(vm_name)
            self.assertEquals(prev_count + 1, len(storage_list))

            # Get cdrom info
            cd_info = inst.vmstorage_lookup(vm_name, cdrom_dev)
            self.assertEquals(u'cdrom', cd_info['type'])
            self.assertEquals(iso_path, cd_info['path'])

            # update path of existing cd with
            # non existent iso
            self.assertRaises(InvalidParameter, inst.vmstorage_update,
                              vm_name, cdrom_dev, {'path': wrong_iso_path})

            # Make sure CD ROM still exists after failure
            cd_info = inst.vmstorage_lookup(vm_name, cdrom_dev)
            self.assertEquals(u'cdrom', cd_info['type'])
            self.assertEquals(iso_path, cd_info['path'])

            # update path of existing cd with existent iso of shutoff vm
            inst.vmstorage_update(vm_name, cdrom_dev, {'path': iso_path2})
            cdrom_info = inst.vmstorage_lookup(vm_name, cdrom_dev)
            self.assertEquals(iso_path2, cdrom_info['path'])

            # update path of existing cd with existent iso of running vm
            inst.vm_start(vm_name)
            inst.vmstorage_update(vm_name, cdrom_dev, {'path': iso_path})
            cdrom_info = inst.vmstorage_lookup(vm_name, cdrom_dev)
            self.assertEquals(iso_path, cdrom_info['path'])

            # eject cdrom
            cdrom_dev = inst.vmstorage_update(vm_name, cdrom_dev, {'path': ''})
            cdrom_info = inst.vmstorage_lookup(vm_name, cdrom_dev)
            self.assertEquals('', cdrom_info['path'])
            inst.vm_poweroff(vm_name)

            # removing non existent cdrom
            self.assertRaises(NotFoundError, inst.vmstorage_delete, vm_name,
                              "fakedev")

            # removing valid cdrom
            inst.vmstorage_delete(vm_name, cdrom_dev)
            storage_list = inst.vmstorages_get_list(vm_name)
            self.assertEquals(prev_count, len(storage_list))

            # Create a new cdrom using a remote iso
            valid_remote_iso_path = utils.get_remote_iso_path()
            cdrom_args = {"type": "cdrom",
                          "path": valid_remote_iso_path}
            cdrom_dev = inst.vmstorages_create(vm_name, cdrom_args)
            storage_list = inst.vmstorages_get_list(vm_name)
            self.assertEquals(prev_count + 1, len(storage_list))

            # Update remote-backed cdrom with the same ISO
            inst.vmstorage_update(vm_name, cdrom_dev,
                                  {'path': valid_remote_iso_path})
            cdrom_info = inst.vmstorage_lookup(vm_name, cdrom_dev)
            cur_cdrom_path = re.sub(":80/", '/', cdrom_info['path'])
            self.assertEquals(valid_remote_iso_path, cur_cdrom_path)

    @unittest.skipUnless(utils.running_as_root(), 'Must be run as root')
    def test_vm_storage_provisioning(self):
        inst = model.Model(objstore_loc=self.tmp_store)

        with RollbackContext() as rollback:
            params = {'name': 'test', 'disks': [{'size': 1, 'pool': {
                      'name': '/plugins/kimchi/storagepools/default'}}],
                      'source_media': UBUNTU_ISO}

            inst.templates_create(params)
            rollback.prependDefer(inst.template_delete, 'test')

            params = {'name': 'test-vm-1',
                      'template': '/plugins/kimchi/templates/test'}
            task = inst.vms_create(params)
            inst.task_wait(task['id'])
            rollback.prependDefer(inst.vm_delete, 'test-vm-1')

            vm_info = inst.vm_lookup(params['name'])
            disk_path = '%s/%s-0.img' % (
                inst.storagepool_lookup('default')['path'], vm_info['uuid'])
            self.assertTrue(os.access(disk_path, os.F_OK))
        self.assertFalse(os.access(disk_path, os.F_OK))

    def _create_template_conf_with_disk_format(self, vol_format):
        if vol_format is None:
            conf_file_data = "[main]\n\n[storage]\n\n[[disk.0]]\n" \
                             "#format = \n\n[graphics]\n\n[processor]\n"
        else:
            conf_file_data = "[main]\n\n[storage]\n\n[[disk.0]]\n" \
                             "format = %s\n\n[graphics]\n\n[processor]\n"\
                             % vol_format

        config_file = os.path.join(paths.sysconf_dir, 'template.conf')
        config_bkp_file = \
            os.path.join(paths.sysconf_dir, 'template.conf-unit_test_bkp')

        os.rename(config_file, config_bkp_file)

        with open(config_file, 'w') as f:
            f.write(conf_file_data)

        osinfo.defaults = osinfo._get_tmpl_defaults()

    def _restore_template_conf_file(self):
        config_file = os.path.join(paths.sysconf_dir, 'template.conf')
        config_bkp_file = \
            os.path.join(paths.sysconf_dir, 'template.conf-unit_test_bkp')
        os.rename(config_bkp_file, config_file)
        osinfo.defaults = osinfo._get_tmpl_defaults()

    def _get_disk_format_from_vm(self, vm, conn):
        dom = VMModel.get_vm(vm, conn)
        xml = dom.XMLDesc(0)
        xpath = "/domain/devices/disk[@device='disk']/driver/@type"
        return xpath_get_text(xml, xpath)[0]

    @unittest.skipUnless(utils.running_as_root(), 'Must be run as root')
    def test_template_get_default_vol_format_from_conf(self):
        inst = model.Model(objstore_loc=self.tmp_store)

        with RollbackContext() as rollback:
            self._create_template_conf_with_disk_format('vmdk')
            rollback.prependDefer(self._restore_template_conf_file)

            params = {'name': 'test', 'disks': [{'size': 1, 'pool': {
                      'name': '/plugins/kimchi/storagepools/default'}}],
                      'source_media': UBUNTU_ISO}
            inst.templates_create(params)
            rollback.prependDefer(inst.template_delete, 'test')

            params = {'name': 'test-vm-1',
                      'template': '/plugins/kimchi/templates/test'}
            task = inst.vms_create(params)
            inst.task_wait(task['id'])
            rollback.prependDefer(inst.vm_delete, 'test-vm-1')

            created_disk_format = self._get_disk_format_from_vm(
                'test-vm-1', inst.conn
            )
            self.assertEqual(created_disk_format, 'vmdk')

    @unittest.skipUnless(utils.running_as_root(), 'Must be run as root')
    def test_template_creates_user_defined_vol_format_instead_default(self):
        inst = model.Model(objstore_loc=self.tmp_store)

        default_vol = 'vmdk'
        user_vol = 'raw'
        with RollbackContext() as rollback:
            self._create_template_conf_with_disk_format(default_vol)
            rollback.prependDefer(self._restore_template_conf_file)

            params = {'name': 'test', 'disks': [{
                'size': 1, 'format': user_vol,
                'pool': {'name': '/plugins/kimchi/storagepools/default'}}],
                'source_media': UBUNTU_ISO}

            inst.templates_create(params)
            rollback.prependDefer(inst.template_delete, 'test')

            params = {'name': 'test-vm-1',
                      'template': '/plugins/kimchi/templates/test'}
            task = inst.vms_create(params)
            inst.task_wait(task['id'])
            rollback.prependDefer(inst.vm_delete, 'test-vm-1')

            created_disk_format = self._get_disk_format_from_vm(
                'test-vm-1', inst.conn
            )
            self.assertEqual(created_disk_format, user_vol)

    @unittest.skipUnless(utils.running_as_root(), 'Must be run as root')
    def test_template_uses_qcow2_format_if_no_user_or_default_defined(self):
        inst = model.Model(objstore_loc=self.tmp_store)

        with RollbackContext() as rollback:
            self._create_template_conf_with_disk_format(None)
            rollback.prependDefer(self._restore_template_conf_file)

            params = {'name': 'test', 'disks': [{'size': 1, 'pool': {
                      'name': '/plugins/kimchi/storagepools/default'}}],
                      'source_media': UBUNTU_ISO}
            inst.templates_create(params)
            rollback.prependDefer(inst.template_delete, 'test')

            params = {'name': 'test-vm-1',
                      'template': '/plugins/kimchi/templates/test'}
            task = inst.vms_create(params)
            inst.task_wait(task['id'])
            rollback.prependDefer(inst.vm_delete, 'test-vm-1')

            created_disk_format = self._get_disk_format_from_vm(
                'test-vm-1', inst.conn
            )
            self.assertEqual(created_disk_format, 'qcow2')

    def test_vm_memory_hotplug(self):
        config.set("authentication", "method", "pam")
        inst = model.Model(None, objstore_loc=self.tmp_store)
        orig_params = {'name': 'test',
                       'memory': {'current': 1024, 'maxmemory': 3072},
                       'source_media': UBUNTU_ISO}
        inst.templates_create(orig_params)

        with RollbackContext() as rollback:
            params = {'name': 'kimchi-vm1',
                      'template': '/plugins/kimchi/templates/test'}
            task1 = inst.vms_create(params)
            inst.task_wait(task1['id'])
            rollback.prependDefer(utils.rollback_wrapper, inst.vm_delete,
                                  'kimchi-vm1')
            # Start vm
            inst.vm_start('kimchi-vm1')
            rollback.prependDefer(utils.rollback_wrapper, inst.vm_poweroff,
                                  'kimchi-vm1')

            # Hotplug memory, only available in Libvirt >= 1.2.14
            params = {'memory': {'current': 2048}}
            if inst.capabilities_lookup()['mem_hotplug_support']:
                inst.vm_update('kimchi-vm1', params)
                rollback.prependDefer(utils.rollback_wrapper, inst.vm_delete,
                                      'kimchi-vm1')
                params['memory']['maxmemory'] = 3072
                self.assertEquals(params['memory'],
                                  inst.vm_lookup('kimchi-vm1')['memory'])
            else:
                self.assertRaises(InvalidOperation, inst.vm_update,
                                  'kimchi-vm1', params)

    def test_vm_edit(self):
        config.set("authentication", "method", "pam")
        inst = model.Model(None,
                           objstore_loc=self.tmp_store)

        # template disk format must be qcow2 because vmsnapshot
        # only supports this format
        orig_params = {
            'name': 'test', 'memory': {'current': 1024},
            'cpu_info': {'vcpus': 1}, 'source_media': UBUNTU_ISO,
            'disks': [{'size': 1, 'format': 'qcow2', 'pool': {
                       'name': '/plugins/kimchi/storagepools/default'}}]}
        inst.templates_create(orig_params)

        with RollbackContext() as rollback:
            params_1 = {'name': 'kimchi-vm1',
                        'template': '/plugins/kimchi/templates/test'}
            params_2 = {'name': 'kimchi-vm2',
                        'template': '/plugins/kimchi/templates/test'}
            task1 = inst.vms_create(params_1)
            inst.task_wait(task1['id'])
            rollback.prependDefer(utils.rollback_wrapper, inst.vm_delete,
                                  'kimchi-vm1')
            task2 = inst.vms_create(params_2)
            inst.task_wait(task2['id'])
            rollback.prependDefer(utils.rollback_wrapper, inst.vm_delete,
                                  'kimchi-vm2')

            vms = inst.vms_get_list()
            self.assertTrue('kimchi-vm1' in vms)

            # make sure "vm_update" works when the domain has a snapshot
            inst.vmsnapshots_create(u'kimchi-vm1')

            # update vm graphics when vm is not running
            inst.vm_update(u'kimchi-vm1',
                           {"graphics": {"passwd": "123456"}})

            inst.vm_start('kimchi-vm1')
            rollback.prependDefer(utils.rollback_wrapper, inst.vm_poweroff,
                                  'kimchi-vm1')

            vm_info = inst.vm_lookup(u'kimchi-vm1')
            self.assertEquals('123456', vm_info['graphics']["passwd"])
            self.assertEquals(None, vm_info['graphics']["passwdValidTo"])

            # update vm graphics when vm is running
            inst.vm_update(u'kimchi-vm1',
                           {"graphics": {"passwd": "abcdef",
                                         "passwdValidTo": 20}})
            vm_info = inst.vm_lookup(u'kimchi-vm1')
            self.assertEquals('abcdef', vm_info['graphics']["passwd"])
            self.assertGreaterEqual(20, vm_info['graphics']['passwdValidTo'])

            info = inst.vm_lookup('kimchi-vm1')
            self.assertEquals('running', info['state'])

            params = {'name': 'new-vm'}
            self.assertRaises(InvalidParameter, inst.vm_update,
                              'kimchi-vm1', params)

            # change VM users and groups, when wm is running.
            inst.vm_update(u'kimchi-vm1',
                           {'users': ['root'], 'groups': ['root']})
            vm_info = inst.vm_lookup(u'kimchi-vm1')
            self.assertEquals(['root'], vm_info['users'])
            self.assertEquals(['root'], vm_info['groups'])

            # change VM users and groups by removing all elements,
            # when vm is running.
            inst.vm_update(u'kimchi-vm1', {'users': [], 'groups': []})
            vm_info = inst.vm_lookup(u'kimchi-vm1')
            self.assertEquals([], vm_info['users'])
            self.assertEquals([], vm_info['groups'])

            # power off vm
            inst.vm_poweroff('kimchi-vm1')
            self.assertRaises(OperationFailed, inst.vm_update,
                              'kimchi-vm1', {'name': 'kimchi-vm2'})

            # update maxvcpus only
            inst.vm_update(u'kimchi-vm1', {'cpu_info': {'maxvcpus': 8}})
            vm_info = inst.vm_lookup(u'kimchi-vm1')
            self.assertEquals(8, vm_info['cpu_info']['maxvcpus'])

            # update vcpus only
            inst.vm_update(u'kimchi-vm1', {'cpu_info': {'vcpus': 4}})
            vm_info = inst.vm_lookup(u'kimchi-vm1')
            self.assertEquals(4, vm_info['cpu_info']['vcpus'])

            # vcpus > maxvcpus: failure
            self.assertRaises(InvalidParameter, inst.vm_update, u'kimchi-vm1',
                              {'cpu_info': {'vcpus': 10}})

            # define CPU topology
            inst.vm_update(u'kimchi-vm1', {'cpu_info': {'topology': {
                           'sockets': 4, 'cores': 2, 'threads': 1}}})
            vm_info = inst.vm_lookup(u'kimchi-vm1')
            self.assertEquals({'sockets': 4, 'cores': 2, 'threads': 1},
                              vm_info['cpu_info']['topology'])

            # vcpus not a multiple of (cores * threads)
            self.assertRaises(InvalidParameter, inst.vm_update, u'kimchi-vm1',
                              {'cpu_info': {'vcpus': 1}})

            # maxvcpus different of (sockets * cores * threads)
            self.assertRaises(InvalidParameter, inst.vm_update, u'kimchi-vm1',
                              {'cpu_info': {'maxvcpus': 4}})

            # topology does not match maxvcpus (8 != 2 * 2 * 1)
            self.assertRaises(InvalidParameter, inst.vm_update, u'kimchi-vm1',
                              {'cpu_info': {'topology': {
                               'sockets': 2, 'cores': 2, 'threads': 1}}})

            # undefine CPU topology
            inst.vm_update(u'kimchi-vm1', {'cpu_info': {'topology': {}}})
            vm_info = inst.vm_lookup(u'kimchi-vm1')
            self.assertEquals({}, vm_info['cpu_info']['topology'])

            # reduce maxvcpus to same as vcpus
            inst.vm_update(u'kimchi-vm1', {'cpu_info': {'maxvcpus': 4}})
            vm_info = inst.vm_lookup(u'kimchi-vm1')
            self.assertEquals(4, vm_info['cpu_info']['maxvcpus'])

            # rename and increase memory when vm is not running
            params = {'name': u'пeω-∨м',
                      'memory': {'current': 2048}}
            inst.vm_update('kimchi-vm1', params)
            rollback.prependDefer(utils.rollback_wrapper, inst.vm_delete,
                                  u'пeω-∨м')
            self.assertEquals(info['uuid'], inst.vm_lookup(u'пeω-∨м')['uuid'])
            info = inst.vm_lookup(u'пeω-∨м')
            # Max memory is returned, add to test
            params['memory']['maxmemory'] = 2048
            for key in params.keys():
                self.assertEquals(params[key], info[key])

            # change only VM users - groups are not changed (default is empty)
            users = inst.users_get_list()[:3]
            inst.vm_update(u'пeω-∨м', {'users': users})
            self.assertEquals(users, inst.vm_lookup(u'пeω-∨м')['users'])
            self.assertEquals([], inst.vm_lookup(u'пeω-∨м')['groups'])

            # change only VM groups - users are not changed (default is empty)
            groups = inst.groups_get_list()[:2]
            inst.vm_update(u'пeω-∨м', {'groups': groups})
            self.assertEquals(users, inst.vm_lookup(u'пeω-∨м')['users'])
            self.assertEquals(groups, inst.vm_lookup(u'пeω-∨м')['groups'])

            # change VM users and groups by adding a new element to each one
            users.append(pwd.getpwuid(os.getuid()).pw_name)
            groups.append(grp.getgrgid(os.getgid()).gr_name)
            inst.vm_update(u'пeω-∨м', {'users': users, 'groups': groups})
            self.assertEquals(users, inst.vm_lookup(u'пeω-∨м')['users'])
            self.assertEquals(groups, inst.vm_lookup(u'пeω-∨м')['groups'])

            # change VM users (wrong value) and groups
            # when an error occurs, everything fails and nothing is changed
            self.assertRaises(InvalidParameter, inst.vm_update, u'пeω-∨м',
                              {'users': ['userdoesnotexist'], 'groups': []})
            self.assertEquals(users, inst.vm_lookup(u'пeω-∨м')['users'])
            self.assertEquals(groups, inst.vm_lookup(u'пeω-∨м')['groups'])

            # change VM users and groups (wrong value)
            # when an error occurs, everything fails and nothing is changed
            self.assertRaises(InvalidParameter, inst.vm_update, u'пeω-∨м',
                              {'users': [], 'groups': ['groupdoesnotexist']})
            self.assertEquals(users, inst.vm_lookup(u'пeω-∨м')['users'])
            self.assertEquals(groups, inst.vm_lookup(u'пeω-∨м')['groups'])

            # change VM users and groups by removing all elements
            inst.vm_update(u'пeω-∨м', {'users': [], 'groups': []})
            self.assertEquals([], inst.vm_lookup(u'пeω-∨м')['users'])
            self.assertEquals([], inst.vm_lookup(u'пeω-∨м')['groups'])

    def test_get_interfaces(self):
        inst = model.Model('test:///default',
                           objstore_loc=self.tmp_store)
        expected_ifaces = netinfo.all_favored_interfaces()
        ifaces = inst.interfaces_get_list()
        self.assertEquals(len(expected_ifaces), len(ifaces))
        for name in expected_ifaces:
            iface = inst.interface_lookup(name)
            self.assertEquals(iface['name'], name)
            self.assertIn('type', iface)
            self.assertIn('status', iface)
            self.assertIn('ipaddr', iface)
            self.assertIn('netmask', iface)

    def test_async_tasks(self):
        class task_except(Exception):
            pass

        def quick_op(cb, message):
            cb(message, True)

        def long_op(cb, params):
            time.sleep(params.get('delay', 3))
            cb(params.get('message', ''), params.get('result', False))

        def abnormal_op(cb, params):
            try:
                raise task_except
            except:
                cb("Exception raised", False)

        def continuous_ops(cb, params):
            cb("step 1 OK")
            time.sleep(2)
            cb("step 2 OK")
            time.sleep(2)
            cb("step 3 OK", params.get('result', True))

        inst = model.Model('test:///default',
                           objstore_loc=self.tmp_store)
        taskid = add_task('', quick_op, inst.objstore, 'Hello')
        inst.task_wait(taskid)
        self.assertEquals(get_task_id(), taskid)
        self.assertEquals('finished', inst.task_lookup(taskid)['status'])
        self.assertEquals('Hello', inst.task_lookup(taskid)['message'])

        taskid = add_task('', long_op, inst.objstore,
                          {'delay': 3, 'result': False,
                           'message': 'It was not meant to be'})
        self.assertEquals(get_task_id(), taskid)
        self.assertEquals('running', inst.task_lookup(taskid)['status'])
        self.assertEquals('OK', inst.task_lookup(taskid)['message'])
        inst.task_wait(taskid)
        self.assertEquals('failed', inst.task_lookup(taskid)['status'])
        self.assertEquals('It was not meant to be',
                          inst.task_lookup(taskid)['message'])
        taskid = add_task('', abnormal_op, inst.objstore, {})
        inst.task_wait(taskid)
        self.assertEquals('Exception raised',
                          inst.task_lookup(taskid)['message'])
        self.assertEquals('failed', inst.task_lookup(taskid)['status'])

        taskid = add_task('', continuous_ops, inst.objstore,
                          {'result': True})
        self.assertEquals('running', inst.task_lookup(taskid)['status'])
        inst.task_wait(taskid, timeout=10)
        self.assertEquals('finished', inst.task_lookup(taskid)['status'])

    @unittest.skipUnless(utils.running_as_root(), 'Must be run as root')
    def test_delete_running_vm(self):
        inst = model.Model(objstore_loc=self.tmp_store)

        with RollbackContext() as rollback:
            params = {'name': u'test', 'disks': [], 'source_media': UBUNTU_ISO}
            inst.templates_create(params)
            rollback.prependDefer(inst.template_delete, 'test')

            params = {'name': u'kīмсhī-∨м',
                      'template': u'/plugins/kimchi/templates/test'}
            task = inst.vms_create(params)
            inst.task_wait(task['id'])
            rollback.prependDefer(utils.rollback_wrapper, inst.vm_delete,
                                  u'kīмсhī-∨м')

            inst.vm_start(u'kīмсhī-∨м')
            self.assertEquals(inst.vm_lookup(u'kīмсhī-∨м')['state'], 'running')
            rollback.prependDefer(utils.rollback_wrapper, inst.vm_poweroff,
                                  u'kīмсhī-∨м')

            inst.vm_delete(u'kīмсhī-∨м')

            vms = inst.vms_get_list()
            self.assertFalse(u'kīмсhī-∨м' in vms)

    @unittest.skipUnless(utils.running_as_root(), 'Must be run as root')
    def test_vm_list_sorted(self):
        inst = model.Model(objstore_loc=self.tmp_store)

        with RollbackContext() as rollback:
            params = {'name': 'test', 'disks': [], 'source_media': UBUNTU_ISO}
            inst.templates_create(params)
            rollback.prependDefer(inst.template_delete, 'test')

            params = {'name': 'kimchi-vm',
                      'template': '/plugins/kimchi/templates/test'}
            task = inst.vms_create(params)
            inst.task_wait(task['id'])
            rollback.prependDefer(inst.vm_delete, 'kimchi-vm')

            vms = inst.vms_get_list()

            self.assertEquals(vms, sorted(vms, key=unicode.lower))

    def test_vm_clone(self):
        inst = model.Model('test:///default', objstore_loc=self.tmp_store)

        all_vm_names = inst.vms_get_list()
        name = all_vm_names[0]

        original_vm = inst.vm_lookup(name)
        if original_vm['state'] == u'shutoff':
            inst.vm_start(name)

        # the VM 'test' should be running by now, so we can't clone it yet
        self.assertRaises(InvalidParameter, inst.vm_clone, name)

        with RollbackContext() as rollback:
            inst.vm_poweroff(name)
            rollback.prependDefer(inst.vm_start, name)

            # create two simultaneous clones of the same VM
            # and make sure both of them complete successfully
            task1 = inst.vm_clone(name)
            task2 = inst.vm_clone(name)
            clone1_name = task1['target_uri'].split('/')[-2]
            rollback.prependDefer(inst.vm_delete, clone1_name)
            clone2_name = task2['target_uri'].split('/')[-2]
            rollback.prependDefer(inst.vm_delete, clone2_name)
            inst.task_wait(task1['id'])
            task1 = inst.task_lookup(task1['id'])
            self.assertEquals('finished', task1['status'])
            inst.task_wait(task2['id'])
            task2 = inst.task_lookup(task2['id'])
            self.assertEquals('finished', task2['status'])

            # update the original VM info because its state has changed
            original_vm = inst.vm_lookup(name)
            clone_vm = inst.vm_lookup(clone1_name)

            self.assertNotEqual(original_vm['name'], clone_vm['name'])
            self.assertTrue(re.match(u'%s-clone-\d+' % original_vm['name'],
                                     clone_vm['name']))
            del original_vm['name']
            del clone_vm['name']

            self.assertNotEqual(original_vm['uuid'], clone_vm['uuid'])
            del original_vm['uuid']
            del clone_vm['uuid']

            # compare all VM settings except the ones already compared
            # (and removed) above (i.e. 'name' and 'uuid')
            self.assertEquals(original_vm, clone_vm)

    def test_use_test_host(self):
        inst = model.Model('test:///default',
                           objstore_loc=self.tmp_store)

        with RollbackContext() as rollback:
            params = {
                'name': 'test',
                'source_media': UBUNTU_ISO,
                'domain': 'test',
                'arch': 'i686',
                'disks': []
            }

            _setDiskPoolDefaultTest()
            rollback.prependDefer(_setDiskPoolDefault)

            inst.templates_create(params)
            rollback.prependDefer(inst.template_delete, 'test')

            params = {'name': 'kimchi-vm',
                      'template': '/plugins/kimchi/templates/test'}
            task = inst.vms_create(params)
            inst.task_wait(task['id'])
            rollback.prependDefer(inst.vm_delete, 'kimchi-vm')

            vms = inst.vms_get_list()

            self.assertTrue('kimchi-vm' in vms)

    def test_get_distros(self):
        inst = model.Model('test:///default',
                           objstore_loc=self.tmp_store)
        distros = inst.distros_get_list()
        for d in distros:
            distro = inst.distro_lookup(d)
            self.assertIn('name', distro)
            self.assertIn('os_distro', distro)
            self.assertIn('os_version', distro)
            self.assertIn('os_arch', distro)
            self.assertIn('path', distro)

    @unittest.skipUnless(utils.running_as_root(), 'Must be run as root')
    def test_deep_scan(self):
        inst = model.Model(None,
                           objstore_loc=self.tmp_store)
        with RollbackContext() as rollback:
            deep_path = os.path.join(TMP_DIR, 'deep-scan')
            subdir_path = os.path.join(deep_path, 'isos')
            if not os.path.exists(subdir_path):
                os.makedirs(subdir_path)
            ubuntu_iso = os.path.join(deep_path, 'ubuntu12.04.iso')
            sles_iso = os.path.join(subdir_path, 'sles10.iso')
            iso_gen.construct_fake_iso(ubuntu_iso, True, '12.04', 'ubuntu')
            iso_gen.construct_fake_iso(sles_iso, True, '10', 'sles')

            args = {'name': 'kimchi-scanning-pool',
                    'path': deep_path,
                    'type': 'kimchi-iso'}
            inst.storagepools_create(args)
            rollback.prependDefer(shutil.rmtree, deep_path)
            rollback.prependDefer(shutil.rmtree, args['path'])
            rollback.prependDefer(inst.storagepool_deactivate, args['name'])

            time.sleep(1)
            volumes = inst.storagevolumes_get_list(args['name'])
            self.assertEquals(len(volumes), 2)


class BaseModelTests(unittest.TestCase):
    class FoosModel(object):
        def __init__(self):
            self.data = {}

        def create(self, params):
            self.data.update(params)

        def get_list(self):
            return list(self.data)

    class TestModel(wok.basemodel.BaseModel):
        def __init__(self):
            foo = BaseModelTests.FoosModel()
            super(BaseModelTests.TestModel, self).__init__([foo])

    def test_root_model(self):
        t = BaseModelTests.TestModel()
        t.foos_create({'item1': 10})
        self.assertEquals(t.foos_get_list(), ['item1'])
