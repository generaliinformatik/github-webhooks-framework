#! /usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (C) 2020 Generali AG, Rene Fuehrer <rene.fuehrer@generali.com>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import time
from sys import stderr, hexversion
import logging
from ipaddress import ip_address, ip_network
import re
import hmac
import json
from json import loads, dumps
from subprocess import Popen, PIPE  # nosec
from tempfile import mkstemp
import os
from os import access, X_OK, remove, fdopen
from os.path import isfile, abspath, normpath, dirname, join, basename
import requests
from flask import Flask, request, abort

# initialize dynamic debug level
logging.basicConfig(stream=stderr, level=logging.INFO)

app = Flask(__name__)


@app.route("/", methods=["GET", "POST"])
def index():  # noqa: C901 - ignore complexity of function
    """
    Main WSGI application entry.
    """
    app_path = os.path.dirname(os.path.abspath(__file__))
    path = normpath(abspath(dirname(__file__)))

    with open(join(path, "config.json"), "r") as cfg:
        config = loads(cfg.read())
        cfg.close()

    if "debug_level_old" not in locals():
        debug_level_old = "INFO"
    debug_level = str(config.get("debug_level", "INFO"))
    if debug_level != debug_level_old:
        if debug_level == "DEBUG":
            logging.getLogger().setLevel(logging.DEBUG)
        elif debug_level == "INFO":
            logging.getLogger().setLevel(logging.INFO)
        elif debug_level == "WARNING":
            logging.getLogger().setLevel(logging.WARNING)
        elif debug_level == "ERROR":
            logging.getLogger().setLevel(logging.ERROR)
        elif debug_level == "CRITICAL":
            logging.getLogger().setLevel(logging.CRITICAL)
        else:
            logging.getLogger().setLevel(logging.INFO)
        logging.info("debug level set dynamically to: %s", debug_level)
        debug_level_old = debug_level

    # Only POST is implemented
    if request.method != "POST":
        abort(501)

    hooks = config.get("hooks_path", join(path, "hooks"))
    if os.path.isdir(config.get("hooks_path", "")):
        logging.debug("hooks path set to: %s", hooks)
    else:
        logging.warning("hooks path not valid: %s", hooks)

    # Allow Github IPs only
    logging.debug("checking valid IPs...")
    # get ip address of requester
    src_ip = ip_address(
        "{}".format(request.access_route[0])  # Fix stupid ipaddress issue
    )

    if config.get("github_ips_only", True):
        whitelist = requests.get("https://api.github.com/meta").json()["hooks"]

        for valid_ip in whitelist:
            if src_ip in ip_network(valid_ip):
                break
        else:
            # pylint: disable=logging-format-interpolation
            logging.error("[403] IP {} not allowed".format(src_ip))
            abort(403)

    logging.debug("checking valid IPs...done.")
    # Enforce secret
    logging.debug("checking webhook secret...")
    secret = config.get("enforce_secret", "")
    if secret:
        # change type of secret
        secret = bytes(secret, "utf-8")
        # Only SHA1 is supported
        header_signature = request.headers.get("X-Hub-Signature")
        if header_signature is None:
            logging.error("403: secret check failed: header mandantory")
            abort(403)

        sha_name, signature = header_signature.split("=")
        if sha_name != "sha1":
            logging.error("501: secret check failed: sha1 mandantory")
            abort(501)

        # HMAC requires the key to be bytes, but data is string
        mac = hmac.new(secret, msg=request.data, digestmod="sha1")

        # Python prior to 2.7.7 does not have hmac.compare_digest
        if hexversion >= 0x020707F0:
            if not hmac.compare_digest(str(mac.hexdigest()), str(signature)):
                logging.warning("[403] secret check failed: hex version wrong")
                abort(403)
        else:
            # What compare_digest provides is protection against timing
            # attacks; we can live without this protection for a web-based
            # application
            if str(mac.hexdigest()) != str(signature):
                logging.warning("[403] secret check failed (ip=%s)", src_ip)
                abort(403)
    logging.debug("checking webhook secret...done.")

    # Implement ping
    event = request.headers.get("X-GitHub-Event", "ping")
    logging.debug("event type detected: %s", event)
    if event == "ping":
        return dumps({"msg": "pong"})

    # Gather data
    try:
        payload = request.get_json()
    except Exception:
        logging.warning("[400] request parsing failed")
        abort(400)

    # Determining the branch is tricky, as it only appears for certain event
    # types an at different levels
    logging.debug("checking branch...")
    branch = None
    try:
        # backup evenry json
        backup_path = config.get("backup_path", "")
        logging.debug("backup path set: %s", backup_path)
        if os.path.exists(backup_path):
            # pylint: disable=line-too-long
            backup_file = (
                config.get("backup_path", path)
                + "/"
                + time.strftime("%Y%m%d-%H%M%S")
                + "-"
                + event
                + ".json"
            )

            logging.debug("backup file set: %s", backup_file)
            with open(backup_file, "w") as this_payloadexport:
                json.dump(payload, this_payloadexport)
                this_payloadexport.close()

        else:
            logging.info("backup not created; backup path not given or invalid")

        # Case 1: a ref_type indicates the type of ref.
        # This true for create and delete events.
        if "ref_type" in payload:
            if payload["ref_type"] == "branch":
                branch = payload["ref"]

        # Case 2: a pull_request object is involved. This is pull_request and
        # pull_request_review_comment events.
        elif "pull_request" in payload:
            # This is the TARGET branch for the pull-request, not the source
            # branch
            branch = payload["pull_request"]["base"]["ref"]

        elif event in ["push"]:
            # Push events provide a full Git ref in 'ref' and not a 'ref_type'.
            branch = payload["ref"].split("/", 2)[2]

    except KeyError:
        # If the payload structure isn't what we expect, we'll live without
        # the branch name
        logging.debug("payload structure not as expected")

    logging.debug("checking branch...done.")

    # All current events have a repository, but some legacy events do not,
    # so let's be safe
    name = payload["repository"]["name"] if "repository" in payload else None

    meta = {"name": name, "branch": branch, "event": event}
    # pylint: disable=logging-format-interpolation
    logging.info("Metadata:\n{}".format(dumps(meta)))

    # Skip push-delete
    if event == "push" and payload["deleted"]:
        # pylint: disable=logging-format-interpolation
        logging.info("skipping push-delete event for {}".format(dumps(meta)))
        return dumps({"status": "skipped"})

    # Possible hooks
    scripts = []
    if branch and name:
        scripts.append(join(hooks, "{event}-{name}-{branch}".format(**meta)))
        scripts.append(join(hooks, "all-{name}-{branch}".format(**meta)))
    if name:
        scripts.append(join(hooks, "{event}-{name}".format(**meta)))
        scripts.append(join(hooks, "all-{name}".format(**meta)))
    scripts.append(join(hooks, "{event}".format(**meta)))
    scripts.append(join(hooks, "all"))

    # Check permissions
    logging.debug("checking executable hook scripts...")
    scripts = [s for s in scripts if isfile(s) and access(s, X_OK)]
    if not scripts:
        return dumps({"status": "nop"})
    logging.debug("checking executable hook scripts...done.")

    # Save payload to temporal file
    osfd, tmpfile = mkstemp()
    with fdopen(osfd, "w") as this_payloadfile:
        this_payloadfile.write(dumps(payload))
        this_payloadfile.close()

    # search for sub hook scripts (e.g. all => all1, all2, all-test, all-function1, ...)
    logging.debug("checking executable child hook scripts...")
    for subhook_script in scripts:
        # remove hooks dir and slashes
        subhook_script = subhook_script.replace(hooks, "")
        subhook_script = subhook_script.replace("/", "")
        files = [
            f
            for f in os.listdir(app_path + "/" + hooks + "/")
            if re.match(rf"{subhook_script}.*", f)
        ]
        for sub_script in files:
            # check if found files are executable (beware of x flag to non exectuable files!)
            sub_script_filename = app_path + "/" + hooks + "/" + sub_script
            if isfile(sub_script_filename) and access(sub_script_filename, X_OK):
                if join(hooks, sub_script) not in scripts:
                    # just add new files to list
                    scripts.append(join(hooks, sub_script))
                    logging.debug("adding child hook '%s'", sub_script)
    logging.debug("checking executable child hook scripts...done.")

    # Run scripts
    ran = {}
    for this_script in scripts:
        this_script = app_path + "/" + this_script
        logging.info("try to execute hook: %s", this_script)

        proc = Popen(  # nosec - if insecure scripts are saved and can be calld by this function you have a bigger problem :( this script requires dynamic and flexible script calling
            [this_script, tmpfile, event], stdout=PIPE, stderr=PIPE
        )
        stdout, stderr = proc.communicate()

        ran[basename(this_script)] = {
            "returncode": proc.returncode,
            "stdout": stdout.decode("utf-8"),
            "stderr": stderr.decode("utf-8"),
        }

        # Log errors if a hook failed
        if proc.returncode != 0:
            # pylint: disable=logging-too-many-args
            logging.error("{} : {} \n{}".format(this_script, proc.returncode, stderr))

    # Remove temporal file
    remove(tmpfile)

    info = config.get("return_scripts_info", False)
    if not info:
        return dumps({"status": "done"})

    output = dumps(ran, sort_keys=True, indent=4)
    logging.info(output)
    return output


if __name__ == "__main__":
    app.run(
        debug=False,
        host="0.0.0.0",  # nosec - binding in docker conatiner to all possible interfaces
        port=5000,
    )
