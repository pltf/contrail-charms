from base64 import b64decode
import os
from socket import gethostbyname, inet_aton, gethostname, gaierror
import struct
from subprocess import check_call, check_output
import netifaces

import time

import apt_pkg
import json
import platform


from charmhelpers.core.hookenv import (
    config,
    related_units,
    relation_ids,
    relation_get,
    status_set,
    leader_get,
    application_version_set,
    log,
    ERROR,
    open_port,
)
from charmhelpers.core.host import write_file
from charmhelpers.core.templating import render

from docker_utils import (
    is_container_launched,
    is_container_present,
    apply_config_in_container,
    launch_docker_image,
    dpkg_version,
    get_docker_image_id,
    load_docker_image,
)


apt_pkg.init()
config = config()


CONTAINER_NAME = "contrail-controller"
CONFIG_NAME = "controller"


def get_ip(iface=None):
    if not iface:
        if hasattr(netifaces, 'gateways'):
            iface = netifaces.gateways()['default'][netifaces.AF_INET][1]
        else:
            data = check_output("ip route | grep ^default", shell=True).split()
            iface = data[data.index('dev') + 1]
    ip = netifaces.ifaddresses(iface)[netifaces.AF_INET][0]['addr']
    return ip


def fix_hostname():
    hostname = gethostname()
    try:
        gethostbyname(hostname)
    except gaierror:
        ip = get_ip()
        check_call(["sed", "-E", "-i", "-e",
            ("/127.0.0.1[[:blank:]]+/a \\\n" + ip + " " + hostname),
            "/etc/hosts"])


def get_analytics_list():
    analytics_ip_list = []
    for rid in relation_ids("contrail-analytics"):
        for unit in related_units(rid):
            ip = relation_get("private-address", unit, rid)
            analytics_ip_list.append(ip)
    sort_key = lambda ip: struct.unpack("!L", inet_aton(ip))[0]
    analytics_ip_list = sorted(analytics_ip_list, key=sort_key)
    return analytics_ip_list


def controller_ctx():
    ctx = {}
    controller_ip_list = []
    for rid in relation_ids("controller-cluster"):
        for unit in related_units(rid):
            ip = relation_get("private-address", unit, rid)
            controller_ip_list.append(ip)
    # add it's own ip address
    controller_ip_list.append(get_ip())
    sort_key = lambda ip: struct.unpack("!L", inet_aton(ip))[0]
    controller_ip_list = sorted(controller_ip_list, key=sort_key)
    ctx["controller_servers"] = controller_ip_list
    return ctx


def analytics_ctx():
    """Get the ipaddres of all contrail nodes"""
    return {"analytics_servers": get_analytics_list()}


def identity_admin_ctx():
    auth_info = config.get("auth_info")
    return (json.loads(auth_info) if auth_info else {})


def decode_cert(key):
    val = config.get(key)
    if not val:
        return None
    try:
        return b64decode(val)
    except Exception as e:
        log("Couldn't decode certificate from config['{}']: {}".format(
            key, str(e)), level=ERROR)
    return None


def get_context():
    ctx = {}
    ctx["auth_mode"] = config.get("auth-mode")
    ctx["cloud_admin_role"] = config.get("cloud-admin-role")
    ctx["global_read_only_role"] = config.get("global-read-only-role")
    info = config.get("orchestrator_info")
    if info:
        ctx.update(json.loads(info))

    ssl_ca = decode_cert("ssl_ca")
    ctx["ssl_ca"] = ssl_ca
    ctx["ssl_cert"] = decode_cert("ssl_cert")
    ctx["ssl_key"] = decode_cert("ssl_key")
    ctx["ssl_enabled"] = (ssl_ca is not None and len(ssl_ca) > 0)

    ctx["db_user"] = leader_get("db_user")
    ctx["db_password"] = leader_get("db_password")

    ctx["rabbitmq_user"] = leader_get("rabbitmq_user")
    ctx["rabbitmq_password"] = leader_get("rabbitmq_password")
    ctx["rabbitmq_vhost"] = leader_get("rabbitmq_vhost")

    ctx.update(controller_ctx())
    ctx.update(analytics_ctx())
    ctx.update(identity_admin_ctx())
    return ctx


def _save_file(path, data):
    if data:
        fdir = os.path.dirname(path)
        if not os.path.exists(fdir):
            os.makedirs(fdir)
        write_file(path, data, perms=0o400)
    elif os.path.exists(path):
        os.remove(path)


def render_config(ctx=None):
    if not ctx:
        ctx = get_context()

    # NOTE: store files in default paths cause no way to pass this path to
    # some of components (sandesh)
    ssl_ca = ctx["ssl_ca"]
    _save_file("/etc/contrailctl/ssl/ca-cert.pem", ssl_ca)
    ssl_cert = ctx["ssl_cert"]
    _save_file("/etc/contrailctl/ssl/server.pem", ssl_cert)
    ssl_key = ctx["ssl_key"]
    _save_file("/etc/contrailctl/ssl/server-privkey.pem", ssl_key)

    render("controller.conf", "/etc/contrailctl/controller.conf", ctx)


def update_charm_status(update_config=True):
    if is_container_launched(CONTAINER_NAME):
        status_set("active", "Unit ready")
        if update_config:
            render_config()
            apply_config_in_container(CONTAINER_NAME, CONFIG_NAME)
        return

    if is_container_present(CONTAINER_NAME):
        status_set(
            "error",
            "Container is present but is not running. Run or remove it.")
        return

    image_id = get_docker_image_id(CONTAINER_NAME)
    if not image_id:
        image_id = load_docker_image(CONTAINER_NAME)
        if not image_id:
            status_set('waiting', 'Awaiting for container resource')
            return

    ctx = get_context()
    missing_relations = []
    if not ctx.get("db_user"):
        # NOTE: Charms don't allow to deploy cassandra in AllowAll mode
        missing_relations.append("contrail-controller-cluster")
    if not ctx.get("analytics_servers"):
        missing_relations.append("contrail-analytics")
    if missing_relations:
        status_set('blocked',
                   'Missing relations: ' + ', '.join(missing_relations))
        return
    if not ctx.get("cloud_orchestrator"):
        status_set('blocked',
                   'Missing cloud orchestrator info in relations.')
        return
    if not ctx.get("keystone_ip"):
        status_set('blocked',
                   'Missing auth info in relation with contrail-auth.')
        return
    # TODO: what should happens if relation departed?

    render_config(ctx)
    for port in ("8082", "8080", "8143"):
        open_port(port, "TCP")

    args = []
    if platform.linux_distribution()[2].strip() == "trusty":
        args.append("--pid=host")
    launch_docker_image(CONTAINER_NAME, args)

    time.sleep(5)
    version = dpkg_version(CONTAINER_NAME, "contrail-control")
    application_version_set(version)
    status_set("active", "Unit ready")
