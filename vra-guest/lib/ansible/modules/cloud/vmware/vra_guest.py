#!/usr/bin/python

ANSIBLE_METADATA = {
    'metadata_version': '0.1',
    'status': ['preview'],
    'supported_by': 'community'
}

DOCUMENTATION = '''
---
module: vra_guest

short_description: Provisioning of a VMware vRA guest from a blueprint

version_added: "2.6"

description:
    - "Create a VM from a Blueprint via vRealizeAutomation (vRA)"

options:
    blueprint_instance_id:
        description:
            - ID of the instance within the Blueprint - there should only ever be a single ID for this module to work
        required: true
    blueprint_name:
        description:
            - Name of the Blueprint to use for provisioning
        required: true
    cpu:
        description:
            - Number of CPUs for the VM (integer)
        required: true
    extra_disks:
        description:
            - Array of additional disks to add to the VM - these are *in addition to* the base/root disk (Disk0 - which cannot be modified)
            - 'Valid attributes are:'
            - ' - C(size_gb) (integer): Disk storage size in specified unit.'
            - ' - C(mount_point) (str): Mount point (Linux) or drive letter (Windows).'
        required: false
    hostname:
        description:
            - Hostname of the VM - note that this requires a custom "Hostname" property be added to the Blueprint
        required: true
    memory:
        description:
            - Amount of memory, in GB (integer)
        required: true
    network_adapter:
        description:
            - Name of the 'Network Adapter' (network) to attach the VM to - this will drive the target network for the VM
        required: true
    vra_hostname:
        description:
            - Hostname of the vRA instance to communicate with
        required: true
    vra_password:
        description:
            - Password of the user interacting with the API
        required: true
    vra_tenant:
        description:
            - Tenant name for the vRA provisioning
        required: true
    vra_username:
        description:
            - Name of the user interacting with the API
        required: true
    wait_timeout:
        description:
          - Number of seconds to wait for a VM to boot on creation
        type: int
        default: 600
        required: false

requirements:
    - copy
    - json
    - requests
    - time

author:
    - Justin Karimi (@jekhokie) <jekhokie@gmail.com>
'''

EXAMPLES = '''
- name: Create a VM from a Blueprint
  delegate_to: localhost
  vra_guest:
    blueprint_instance_id: "vSphere__vCenter__Machine_1"
    blueprint_name: "Linux"
    cpu: 2
    extra_disks:
        - size_gb: 60
          mount_point: "/mnt1"
        - size_gb: 80
          mount_point: "/mnt2"
    hostname: "Test-VM"
    memory: 4096
    network_adapter: "network-adapter-name"
    vra_hostname: "my-vra-host.localhost"
    vra_password: "super-secret-pass"
    vra_tenant: "vsphere.local"
    vra_username: "automation-user"
    wait_timeout: 300
'''

RETURN = '''
instance:
    description: Metadata about the newly-created VM
    type: dict
    returned: always
    sample: none
'''

import copy
import json
import requests
import time
from ansible.module_utils.basic import AnsibleModule
from requests.packages.urllib3.exceptions import InsecureRequestWarning

# ignore annoyances
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

class VRAHelper(object):
    '''Helper class for managing interaction with vRA and corresponding resources.'''

    def __init__(self, module):
        """
        Default constructor
        Args:
            module: object containing parameters passed by playbook

        Returns: (VRAHelper) Instance of the VRAHelper class
        """
        self.module = module
        self.blueprint_instance_id = module.params['blueprint_instance_id']
        self.blueprint_name = module.params['blueprint_name']
        self.cpu = module.params['cpu']
        self.extra_disks = module.params['extra_disks']
        self.hostname = module.params['hostname']
        self.memory = module.params['memory']
        self.network_adapter = module.params['network_adapter']
        self.vra_hostname = module.params['vra_hostname']
        self.vra_password = module.params['vra_password']
        self.vra_tenant = module.params['vra_tenant']
        self.vra_username = module.params['vra_username']

        self.ip = None
        self.headers = {
            "accept": "application/json",
            "content-type": "application/json"
        }

        # initialize bearer token for auth
        self.get_auth()

    def get_auth(self):
        """
        Get a bearer token and update the instance headers for authorization

        Returns: None (updates the instance headers with token)
        """
        try:
            url = "https://%s/identity/api/tokens" % (self.vra_hostname)
            payload = '{"username":"%s","password":"%s","tenant":"%s"}' % (self.vra_username, self.vra_password, self.vra_tenant)
            response = requests.request("POST", url, data=payload, headers=self.headers, verify=False)

            # format bearer token into correct auth pattern
            token = response.json()['id']
            self.headers["authorization"] = "Bearer %s" % token
        except Exception as e:
            self.module.fail_json(msg="Failed to get bearer token: %s" % (e))

    def get_catalog_id(self):
        """
        Retrieve the catalog ID for the Blueprint requested

        Returns: None (updates the instance with the catalog ID)
        """
        catalog_dict = {}

        try:
            url = "https://%s/catalog-service/api/consumer/entitledCatalogItems" % (self.vra_hostname)
            response = requests.request("GET", url, headers=self.headers, verify=False)

            for i in response.json()['content']:
                item_name = i['catalogItem']['name']
                item_id = i['catalogItem']['id']
                catalog_dict[item_name] = item_id

            self.catalog_id = catalog_dict[self.blueprint_name]
        except Exception as e:
            self.module.fail_json(msg="Failed to get catalog ID for blueprint %s: %s" % (self.blueprint_name, e))

    def get_template_json(self):
        """
        Retrieve a template JSON object for the Blueprint being requested

        Returns: None (updates the instance with the template JSON object)
        """
        try:
            url = "https://%s/catalog-service/api/consumer/entitledCatalogItems/%s/requests/template" % (self.vra_hostname, self.catalog_id)
            response = requests.request("GET", url, headers=self.headers, verify=False)

            self.template_json = response
        except Exception as e:
            self.module.fail_json(msg="Failed to get template JSON for creating the VM: %s" % (e))

    def customize_template(self):
        """
        Customize the Blueprint template for the customizations requested by the playbook

        Returns: None (updates the instance template JSON with customizations)
        """
        template = dict(self.template_json.json())
        metadata = template['data'][self.blueprint_instance_id]['data']
        metadata['cpu'] = self.cpu
        metadata['memory'] = self.memory
        metadata['Hostname'] = self.hostname
        metadata['VirtualMachine.Network0.Name'] = self.network_adapter

        # add custom additional disk drives if requested
        if len(self.extra_disks) >= 1:
            disk_meta_orig = copy.deepcopy(metadata['disks'][0])
            disk_id = disk_meta_orig['data']['id']

            for i, disk in enumerate(self.extra_disks):
                disk_id += 1
                disk_meta = copy.deepcopy(disk_meta_orig)
                disk_meta['data']['capacity'] = self.extra_disks[i]['size_gb']
                disk_meta['data']['label'] = "Hard disk %s" % (i + 2)
                disk_meta['data']['volumeId'] = (i + 1)
                disk_meta['data']['id'] = disk_id
                disk_meta['data']['userCreated'] = "true"
                disk_meta['data']['is_clone'] = "false"
                disk_meta['data']['initial_location'] = self.extra_disks[i]['mount_point']
                metadata['disks'].append(disk_meta)

        self.template_json = template

    def create_vm_from_template(self):
        """
        Make a request to provision a VM with the custom template specified

        Returns: None (updates the instance with the request ID)
        """
        try:
            url = "https://%s/catalog-service/api/consumer/entitledCatalogItems/%s/requests" % (self.vra_hostname, self.catalog_id)
            response = requests.request("POST", url, headers=self.headers, data=json.dumps(self.template_json), verify=False)

            self.request_id = response.json()['id']
        except Exception as e:
            self.module.fail_json(msg="Failed to create VM from template: %s" % (e))

    def get_vm(self):
        """
        Make a request for the details of a VM having a specific hostname

        Returns: None (updates the instance with the VM details)
        """
        try:
            url = "https://%s/catalog-service/api/consumer/resources/types/Infrastructure.Virtual/?limit=5000" % (self.vra_hostname)
            response = requests.request("GET", url, headers=self.headers, verify=False)

            vms = [i for i in response.json()['content'] if i['name'] == self.hostname]
            if len(vms) > 1:
                self.module.fail_json(msg="Duplicate VMs with hostname %s." % (self.hostname))
            elif len(vms) == 1:
                self.request_id = vms[0]['requestId']

                # get details about the VM
                url = "https://%s/catalog-service/api/consumer/requests/%s/resources" % (self.vra_hostname, self.request_id)
                response = requests.request("GET", url, headers=self.headers, verify=False)

                # get the Destroy ID and VM Name using list comprehension
                meta_dict = [element for element in response.json()['content'] if element['providerBinding']['providerRef']['label'] == 'Infrastructure Service'][0]
                self.destroy_id = meta_dict['id']

                # get the VM IP address using list comprehension
                vm_data = [element for element in meta_dict['resourceData']['entries'] if element['key'] == 'ip_address'][0]
                self.ip = vm_data['value']['value']
        except Exception as e:
            self.module.fail_json(msg="Failed to get VM details for '%s': %s" % (self.hostname, e))

    def get_vm_state(self):
        """
        Make a request to find the state of a VM (running, stopped, etc.). This needs to happen
        via the resourceData attribute search due to the fact that the top-level resource request
        via the "get_vm" function above does not return the requestData dictionary that contains the
        required state information.

        Options are:
        - On
        - TurningOn
        - TurningOff
        - Off
        - Rebooting

        Returns: None (updates the instance with the VM state information)
        """
        try:
            if self.request_id == None:
                self.module.fail_json(msg="No request ID to request VM state information for")

            url = "https://%s/catalog-service/api/consumer/resources/%s" % (self.vra_hostname, self.destroy_id)
            response = requests.request("GET", url, headers=self.headers, verify=False)

            meta_dict = [element for element in response.json()['resourceData']['entries'] if element['key'] == 'MachineStatus'][0]
            self.state = meta_dict['value']['value']
        except Exception as e:
            self.module.fail_json(msg="Failed to get VM state information for '%s': %s" % (self.hostname, e))

    def get_vm_build_status(self):
        """
        Check on the status of a VM that had been requested to be built

        Returns: None (updates the instance with the VM build details)
        """
        try:
            url = "https://%s/catalog-service/api/consumer/requests/%s" % (self.vra_hostname, self.request_id)
            response = requests.request("GET", url, headers=self.headers, verify=False)

            self.build_status = response.json()['stateName']
            explanation = response.json()['requestCompletion']
            if explanation is None:
                self.build_explanation = ""
            else:
                self.build_explanation = explanation['completionDetails']
        except Exception as e:
            self.module.fail_json(msg="Failed to get VM create status: %s" % (e))

def run_module():
    # available options for the module
    module_args = dict(
        blueprint_instance_id=dict(type='str', required=True),
        blueprint_name=dict(type='str', required=True),
        cpu=dict(type='int', required=True),
        extra_disks=dict(type='list', default=[]),
        hostname=dict(type='str', required=True),
        memory=dict(type='int', required=True),
        network_adapter=dict(type='str', required=True),
        vra_hostname=dict(type='str', required=True),
        vra_password=dict(type='str', required=True, no_log=True),
        vra_tenant=dict(type='str', required=True),
        vra_username=dict(type='str', required=True),
        wait_timeout=dict(type='int', default=600)
    )

    # seed result dict that is returned
    result = dict(
        changed=False,
        failed=False,
        state='',
        destroy_id='',
        ip='',
        hostname=''
    )

    # default Ansible constructor
    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=True
    )

    # initialize the interface and get a bearer token
    vra_helper = VRAHelper(module)
    vra_helper.get_vm()

    # check mode - see whether the VMs need to be created
    if module.check_mode:
        if vra_helper.ip == None:
            result['changed'] = True

        module.exit_json(**result)

    # only create the VM if it doesn't already exist
    if vra_helper.ip == None:
        result['changed'] = True
        vra_helper.get_catalog_id()
        vra_helper.get_template_json()
        vra_helper.customize_template()
        vra_helper.create_vm_from_template()

        timer = 0
        timeout_seconds = module.params['wait_timeout']
        while True:
            vra_helper.get_vm_build_status()

            if timer >= timeout_seconds:
                module.fail_json(msg="Failed to create VM in %s seconds" % (module.params['wait_timeout']))
            elif vra_helper.build_status == 'Failed':
                module.fail_json(msg="Failed to create VM: %s" % vra_helper.build_explanation)
            elif vra_helper.build_status == 'Successful':
                break

            time.sleep(15)
            timer += 15

        vra_helper.get_vm()

    # assign results for output
    vra_helper.get_vm_state()
    result['state'] = vra_helper.state
    result['destroy_id'] = vra_helper.destroy_id
    result['ip'] = vra_helper.ip
    result['hostname'] = vra_helper.hostname

    # successful run
    module.exit_json(**result)

def main():

    fields = {
        "state": {
            "default": "present",
            "choices": ["absent", "present", "running" "stopped", "restarted" ],
            "type": 'str'
        },
    }

    choice_map = {
        # The function set_vm_state should be created
        "absent": set_vm_state("absent"),
        "present": set_vm_state("present"),
        "running": set_vm_state("running"),
        "stopped": set_vm_state("stopped"),
        "restarted": set_vm_state("restarted")
    }

    module = AnsibleModule(argument_spec=fields)
    is_error, has_changed, result = choice_map.get(
        module.params['state'])(module.params)

    if not is_error:
        module.exit_json(changed=has_changed, meta=result)
    else:
        module.fail_json(msg="Error deleting repo", meta=result)


if __name__ == '__main__':
    main()
