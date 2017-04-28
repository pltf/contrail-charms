from socket import gethostbyname, inet_aton
import struct

import time

import apt_pkg
import json
import yaml
import platform


from charmhelpers.core.hookenv import (
    config,
    related_units,
    relation_ids,
    relation_get,
    unit_get,
    status_set,
    application_version_set,
)
from charmhelpers.core.templating import render

from docker_utils import (
    is_container_launched,
    is_container_present,
    apply_config_in_container,
    launch_docker_image,
    dpkg_version,
    get_docker_image_id
)


apt_pkg.init()
config = config()


CONTAINER_NAME = "contrail-controller"
CONFIG_NAME = "controller"


def get_analytics_list():
    analytics_ip_list = []
    for rid in relation_ids("contrail-analytics"):
        for unit in related_units(rid):
            ip = gethostbyname(relation_get("private-address", unit, rid))
            analytics_ip_list.append(ip)
    sort_key = lambda ip: struct.unpack("!L", inet_aton(ip))[0]
    analytics_ip_list = sorted(analytics_ip_list, key=sort_key)
    return analytics_ip_list


def controller_ctx():
    ctx = {}
    ctx["cloud_orchestrator"] = config.get("cloud_orchestrator")
    ctx["default_log_level"] = config.get("log_level")
    ctx["multi_tenancy"] = config.get("multi_tenancy")

    controller_ip_list = []
    for rid in relation_ids("controller-cluster"):
        for unit in related_units(rid):
            ip = gethostbyname(relation_get("private-address", unit, rid))
            controller_ip_list.append(ip)
    # add it's own ip address
    controller_ip_list.append(gethostbyname(unit_get("private-address")))
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


def get_context():
    ctx = {}
    ctx.update(controller_ctx())
    ctx.update(analytics_ctx())
    ctx.update(identity_admin_ctx())
    return ctx


def render_config(ctx=None):
    if not ctx:
        ctx = get_context()
    render("controller.conf", "/etc/contrailctl/controller.conf", ctx)


def update_charm_status(update_config=True):
    if is_container_launched(CONTAINER_NAME):
        status_set("active", "Unit ready")
        if update_config:
            render_config()
            try:
                apply_config_in_container(CONTAINER_NAME, CONFIG_NAME)
            except Exception:
                pass
        return

    if is_container_present(CONTAINER_NAME):
        status_set(
            "error",
            "Container is present but is not running. Run or remove it.")
        return

    image_id = get_docker_image_id(CONTAINER_NAME)
    if not image_id:
        status_set('waiting', 'Awaiting for container resource')
        return

    ctx = get_context()
    missing_relations = []
    if not ctx.get("analytics_servers"):
        missing_relations.append("contrail-analytics")
    if missing_relations:
        status_set('waiting',
                   'Missing relations: ' + ', '.join(missing_relations))
        return
    if not ctx.get("keystone_ip"):
        status_set('waiting',
                   'Missing auth info in relation with contrail-auth.')
        return
    # TODO: what should happens if relation departed?

    render_config(ctx)
    args = []
    if platform.linux_distribution()[2].strip() == "trusty":
        args.append("--pid=host")
    launch_docker_image(CONTAINER_NAME, args)
    # TODO: find a way to do not use 'sleep'
    time.sleep(5)

    version = dpkg_version(CONTAINER_NAME, "contrail-control")
    application_version_set(version)
    status_set("active", "Unit ready")
