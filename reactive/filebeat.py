import charms.apt
from charms.layer import status
from charms.reactive import when
from charms.reactive import when_not
from charms.reactive import set_state
from charms.reactive import remove_state
from charms.reactive import hook
from charms.reactive.helpers import data_changed
from charms.templating.jinja2 import render
from charms.reactive.flags import any_flags_set

from charmhelpers.core import unitdata
from charmhelpers.core.hookenv import config
from charmhelpers.core.host import restart_on_change, service_stop
from charmhelpers.core.host import file_hash, service, service_running

from elasticbeats import (
    enable_beat_on_boot,
    get_package_candidate,
    push_beat_index,
    remove_beat_on_boot,
    render_without_context,
)

import base64
import os
import time


FILEBEAT_CONFIG = '/etc/filebeat/filebeat.yml'
LOGSTASH_SSL_CERT = '/etc/ssl/certs/filebeat-logstash.crt'
LOGSTASH_SSL_KEY = '/etc/ssl/private/filebeat-logstash.key'


@hook("update-status")
def send_status():
    if any_flags_set('logstash.connected', 'elasticsearch.connected', 'kafka.ready'):
        if service_running("filebeat"):
            status.active("Filebeat ready.")
        else:
            status.blocked("Filebeat service not running.")


@when_not('apt.installed.filebeat')
def install_filebeat():
    # Our layer options will initially install filebeat, so just set a
    # message while we wait for the apt layer to do its thing.
    status.maint('Preparing to install filebeat.')


@when('apt.installed.filebeat')
@when('filebeat.reinstall')
def blocked_until_reinstall():
    """Block until the operator handles a pending reinstall."""
    ver = unitdata.kv().get('filebeat.candidate.version', False)
    if ver:
        msg = "Install filebeat-{} with the 'reinstall' action.".format(ver)
        status.blocked(msg)


@when('beat.render')
@when('apt.installed.filebeat')
@restart_on_change({
    LOGSTASH_SSL_CERT: ['filebeat'],
    LOGSTASH_SSL_KEY: ['filebeat'],
    })
def render_filebeat_template():
    cfg_original_hash = file_hash(FILEBEAT_CONFIG)
    connections = render_without_context('filebeat.yml', FILEBEAT_CONFIG)
    cfg_new_hash = file_hash(FILEBEAT_CONFIG)

    # Ensure ssl files match config each time we render a new template
    manage_filebeat_logstash_ssl()
    remove_state('beat.render')

    if connections:
        if cfg_original_hash != cfg_new_hash:
            service('restart', 'filebeat')
        send_status()
    else:
        # NB: beat base layer will handle waiting status when not connected
        service('stop', 'filebeat')


def manage_filebeat_logstash_ssl():
    """Manage the ssl cert/key that filebeat uses to connect to logstash.

    Create the cert/key files when both logstash_ssl options have been set;
    update when either config option changes; remove if either gets unset.
    """
    logstash_ssl_cert = config().get('logstash_ssl_cert')
    logstash_ssl_key = config().get('logstash_ssl_key')
    if logstash_ssl_cert and logstash_ssl_key:
        cert = base64.b64decode(logstash_ssl_cert).decode('utf8')
        key = base64.b64decode(logstash_ssl_key).decode('utf8')

        if data_changed('logstash_cert', cert):
            render(template='{{ data }}',
                   context={'data': cert},
                   target=LOGSTASH_SSL_CERT, perms=0o444)
        if data_changed('logstash_key', key):
            render(template='{{ data }}',
                   context={'data': key},
                   target=LOGSTASH_SSL_KEY, perms=0o400)
    else:
        if not logstash_ssl_cert and os.path.exists(LOGSTASH_SSL_CERT):
            os.remove(LOGSTASH_SSL_CERT)
        if not logstash_ssl_key and os.path.exists(LOGSTASH_SSL_KEY):
            os.remove(LOGSTASH_SSL_KEY)


@when('apt.installed.filebeat')
@when_not('filebeat.autostarted')
def enlist_filebeat():
    enable_beat_on_boot('filebeat')
    set_state('filebeat.autostarted')


@when('apt.installed.filebeat')
@when('elasticsearch.available')
@when_not('filebeat.index.pushed')
def push_filebeat_index(elasticsearch):
    """Create the Filebeat index in Elasticsearch.

    Once elasticsearch is available, make 5 attempts to create a filebeat
    index. Set appropriate charm status so the operator knows when ES is
    configured to accept data.
    """
    hosts = elasticsearch.list_unit_data()
    for host in hosts:
        host_string = "{}:{}".format(host['host'], host['port'])

    max_attempts = 5
    for i in range(1, max_attempts):
        if push_beat_index(elasticsearch=host_string,
                           service='filebeat', fatal=False):
            set_state('filebeat.index.pushed')
            send_status()
            break
        else:
            msg = "Attempt {} to push filebeat index failed (retrying)".format(i)
            status.waiting(msg)
            time.sleep(i * 30)  # back off 30s for each attempt
    else:
        msg = "Failed to push filebeat index to http://{}".format(host_string)
        status.blocked(msg)


@when('apt.installed.filebeat')
@when('config.changed.install_sources')
def change_filebeat_repo():
    """Set a flag when the apt repo changes."""
    # NB: we can't check for new versions yet because we cannot be sure that
    # the apt update has completed. Set status and a flag to check later.
    status.maint('Pending scan for apt repo changes.')
    set_state('filebeat.repo.changed')


@when('apt.installed.filebeat')
@when('filebeat.repo.changed')
@when_not('apt.needs_update')
def check_filebeat_repo():
    """Check the apt repo for filebeat changes."""
    ver = get_package_candidate('filebeat')
    if ver:
        unitdata.kv().set('filebeat.candidate.version', ver)
        set_state('filebeat.reinstall')
    else:
        unitdata.kv().unset('filebeat.candidate.version')
        remove_state('filebeat.reinstall')
    remove_state('filebeat.repo.changed')


@hook('stop')
def remove_filebeat():
    """Stop, purge, and remove all traces of filebeat."""
    status.maint('Removing filebeat.')
    service_stop('filebeat')
    try:
        os.remove(FILEBEAT_CONFIG)
    except OSError:
        pass
    charms.apt.purge('filebeat')
    remove_beat_on_boot('filebeat')
    remove_state('filebeat.autostarted')
