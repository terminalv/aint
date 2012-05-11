#!/usr/bin/python

"""Creates an instance of the requested type, then configures it once it's runnning."""

import os
import sys
import boto
import time
import subprocess as subp
import os.path as opath
import socket
import logging
import itertools as itt
import aws.setup_dns as sdns
import aws.runcmd as rc

import settings as awssett

logging.basicConfig(level=logging.INFO)

log = logging.getLogger("initialize_instance")

# maps the instance size to the machine image we want to use
# (32 vs 64 bit) (since the smaller instance sizes have to be 32-bit)
ami_32bit = "ami-ab36fbc2" # 32-bit, $0.17/hour, 4 cores, 1.7.GB
ami_64bit = "ami-ad36fbc4" # 64-bit, $0.34/hour, 2 cores, 7.5GB
instance_ami_map = {
    "m1.xlarge": ami_64bit,
    "m2.4xlarge": ami_64bit,
    "m1.large": ami_64bit,
    "c1.medium": ami_32bit,
    "m1.small": ami_32bit,
    }

instance_types = {
    "default": "m1.large",
    "web": 'c1.medium',
    "database": "m2.4xlarge",
    "staging": 'm1.large',
    "rabbitmq": "m1.small",
    "jenkins": "m1.small",
    }

def start_instance(ec2, instance_type=instance_types["default"]):
    ami = instance_ami_map[instance_type]

    log.info("Requesting AMI %s for instance type %s", ami, instance_type)
    # RES = reservation handle, a group of instances
    # including the one we just started (since you can start many at a time)
    res = ec2.run_instances(ami, instance_type=instance_type, 
                            placement='us-east-1d', 
                            key_name='memrise')

    res_id = res.id
    instance = res.instances[0]

    log.info("Waiting for instance %s to start", instance.id)

    def is_running():
        instance.update()
        return instance.state == "running"
    rc.wait(is_running)

    log.info("Instance %s started", instance.id)
    log.info("Connect using host name %s", instance.public_dns_name)

    return instance

def wait_for_ssh(instance):
    user_host = "ubuntu@%s" % instance.public_dns_name

    log.info("Removing stale ssh host keys")
    subp.call(["ssh-keygen", "-f", opath.expanduser("~/.ssh/known_hosts"), "-R", instance.public_dns_name], 
              stderr=open("/dev/null", "w"))

    addrs = set()
    for p, q, r, s, (addr, port) in socket.getaddrinfo(instance.public_dns_name, 0):
        if addr in addrs:
            continue

        subp.call(["ssh-keygen", "-f", opath.expanduser("~/.ssh/known_hosts"), "-R", addr])
        addrs.add(addr)

    log.info("Waiting for ssh to start on %s", instance.public_dns_name)

    rc.wait(lambda: subp.call(["ssh", "-i", memrise_pem, user_host, "true"]))

def set_dns_cname(instance, host_name):
    dns_name = ".".join((host_name, "memrise.com."))
    log.info("Setting address %s as an alias for %s", dns_name, instance.public_dns_name)

    r53 = sdns.connect()
    rrs = sdns.current_rrs(r53)
    sdns.replace_rr(rrs, dns_name, "CNAME", [instance.public_dns_name])
    rrs.commit()

def configure_instance(instance, host_name, instance_type="default"):
    user_host = "ubuntu@%s" % instance.public_dns_name

    instance.add_tag("instance_type", instance_type)
    instance.add_tag("Name", host_name)
    wait_for_ssh(instance)

    log.info("Copying configuration scripts to %s", user_host)
    subp.check_call(["rsync", "-a", "--exclude", ".git/", "-e", "ssh -i" + memrise_pem, ".", "%s:setup_ec2" % user_host])

    log.info("Configuring %s as %s", user_host, instance_type)
    subp.check_call(["ssh", "-i", memrise_pem, user_host, "-t", 
                     "cd setup_ec2/aws && python setup_ec2.py %s %s" % (instance_type, host_name)])

    set_dns_cname(instance, host_name)

def display_keys(user_host):
    log.warn("displaying the new ssh keys for unfuddle/github.")
    subp.check_call(["ssh", "-i", memrise_pem, "-p", "2000", user_host, "cd setup_ec2/aws && python setup_web.py keys"])
    raw_input("configure these and hit [enter] to proceed.")

def create_database_storage(ec2):
    vols = []
    for i in xrange(4):
        vol = ec2.create_volume(50, "us-east-1d")
        vols.append(vol.id)

    return vols

def attach_volumes(ec2, instance, volumes):
    for i, vol in enumerate(volumes):
        device = "/dev/sd%s" % chr((ord("h") + i))
        ec2.attach_volume(vol, instance.id, device)

def start_web_instance(ec2, host_name):
    instance = start_instance(ec2, instance_type=instance_types["web"])
    configure_instance(instance, host_name, "web")

    log.info("Starting web instance configuration")
    subp.check_call(["ssh", "-i", memrise_pem, "-p", "2000", "ubuntu@" + instance.public_dns_name,
                     "cd setup_ec2/aws && python setup_web.py web"])

def start_celery_instance(ec2, host_name):
    instance = start_instance(ec2, instance_type="m1.small")
    configure_instance(instance, host_name, "celery")

    log.info("Starting celery instance configuration")
    subp.check_call(["ssh", "-i", memrise_pem, "-p", "2000", "ubuntu@" + instance.public_dns_name,
                     "cd setup_ec2/aws && python setup_web.py celery"])


def start_jenkins_instance(ec2, host_name):
    instance = start_instance(ec2, instance_type=instance_types["jenkins"])
    configure_instance(instance, host_name, "jenkins")

    log.info("Starting jenkins instance configuration")
    subp.check_call(["ssh", "-i", memrise_pem, "-p", "2000", "ubuntu@" + instance.public_dns_name,
                     "cd setup_ec2/aws && python setup_web.py jenkins"])

def start_rabbitmq_instance(ec2, host_name):
    instance = start_instance(ec2, instance_type=instance_types["rabbitmq"])
    configure_instance(instance, host_name, "rabbitmq")

def start_database_instance(ec2, host_name):
    instance = start_instance(ec2, instance_types["database"])
    volumes = create_database_storage(ec2)
    attach_volumes(ec2, instance, volumes)
    configure_instance(instance, host_name, "mysql")

def start_staging_instance(ec2, host_name):
    instance = start_instance(ec2, instance_types["staging"])
    configure_instance(instance, host_name, "staging")
    subp.check_call(["ssh", "-i", memrise_pem, "-p", "2000", "ubuntu@" + instance.public_dns_name,
                     "cd setup_ec2/aws && python setup_web.py web"])

def start_backupdb_instance(ec2, host_name):
    instance = start_instance(ec2, instance_type=instance_types["database"])
    configure_instance(instance, host_name, "backupdb")

server_type_map = {"web": start_web_instance,
                   "celery": start_celery_instance,
                   "staging": start_staging_instance,
                   "jenkins": start_jenkins_instance,
                   "rabbitmq": start_rabbitmq_instance,
                   "backupdb": start_backupdb_instance,
                   "mysql": start_database_instance}

def main():
    """
    e.g. ../venv/bin/python start_instancepy web web3
    """
    server_type = server_type_map[sys.argv[1]]
    host_name = sys.argv[2]
    os.chmod(memrise_pem, 0600)

    ec2 = boto.connect_ec2()
    server_type(ec2, host_name)
