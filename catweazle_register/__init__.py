#!/usr/bin/env python3

import argparse
import logging
import os
import subprocess
import sys
import stat
import time

import httpx


def main():
    parser = argparse.ArgumentParser(
        description="Register System based on information provided by CatWeazle. "
        "Scripts in /etc/catweazle/register.d will be executed in order. "
        "Scripts will be called with the fqdn as first, and the otp as second argument."
    )

    parser.add_argument(
        "--endpoint",
        dest="endpoint",
        action="store",
        required=True,
        help="CatWeazle endpoint URL",
    )

    parser.add_argument(
        "--retry",
        dest="retry",
        action="store",
        required=False,
        default=10,
        type=int,
        help="Number of retries for fetching CatWeazle data",
    )

    parser.add_argument(
        "--pre_sleep",
        dest="pre_sleep",
        action="store",
        required=False,
        default=30,
        type=int,
        help="wait specified number of seconds, before doing anything. this might be needed"
        "because of replication delay between IdM servers.",
    )

    parser.add_argument(
        "--no_otp_ok",
        dest="no_otp_ok",
        action="store_true",
        required=False,
        default=False,
        help="ignore missing otp, in case catweazle is only doing DNS registration.",
    )

    parsed_args = parser.parse_args()

    register = Register(
        endpoint=parsed_args.endpoint,
        retry=parsed_args.retry,
        pre_sleep=parsed_args.pre_sleep,
        no_otp_ok=parsed_args.no_otp_ok,
    )
    register.run()


class Register:
    def __init__(self, endpoint, retry, pre_sleep, no_otp_ok):
        self.log = logging.getLogger("application")
        self.log.setLevel(logging.DEBUG)
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter("%(levelname)s - %(message)s")
        handler.setFormatter(formatter)
        self.log.addHandler(handler)

        self._endpoint = endpoint
        self._fqdn = None
        self._instance_id = None
        self._no_otp_ok = no_otp_ok
        self._otp = None
        self._pre_sleep = pre_sleep
        self._retry = retry

    @property
    def endpoint(self):
        return self._endpoint

    @property
    def fqdn(self):
        return self._fqdn

    @property
    def instance_id(self):
        if not self._instance_id:
            self._instance_id = httpx.get(
                "http://169.254.169.254/latest/meta-data/instance-id"
            ).text
        return self._instance_id

    @property
    def no_otp_ok(self):
        return self._no_otp_ok

    @property
    def otp(self):
        if not self._otp:
            return "NO_OTP"
        return self._otp

    @property
    def pre_sleep(self):
        return self._pre_sleep

    @property
    def retry(self):
        return self._retry

    def _run_cmd(self, args):
        self.log.info(f"running command: {args}")
        p = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
        for line in p.stdout:
            self.log.info(line.rstrip())
        p.stdout.close()
        self.log.info(f"finished running command: {args}")
        return p.wait()

    def get_cw_data(self):
        self.log.info("Getting CatWeazle Data")
        for _ in range(self.retry):
            self.log.info("Trying to fetch CatWeazle data")
            resp = httpx.get(f"{self.endpoint}/api/v2/instances/{self.instance_id}")
            status_code = resp.status_code
            if status_code is 200:
                data = resp.json()
            else:
                self.log.warning(
                    f"Could not fetch instance data, http status was {status_code}, falling back to v1 api"
                )
                resp = httpx.get(f"{self.endpoint}/api/v1/instances/{self.instance_id}")
                status_code = resp.status_code
                if status_code is 200:
                    data = resp.json()["data"]
                else:
                    self.log.warning(
                        f"Could not fetch instance data, http status was {status_code}, sleeping for 5 seconds"
                    )
                    time.sleep(5)
                    continue
            if "ipa_otp" in data["data"]:
                self.log.info("Success fetching CatWeazle data")
                self._fqdn = data["data"]["fqdn"]
                self._otp = data["data"]["ipa_otp"]
                self.log.info("Getting CatWeazle Data, done")
                return
            elif self.no_otp_ok:
                self.log.info("Success fetching CatWeazle data")
                self.log.info("no otp present, ignoring")
                self._fqdn = data["data"]["fqdn"]
                self.log.info("Getting CatWeazle Data, done")
                return
            else:
                self.log.warning(
                    "instance data incomplete, otp token missing, sleeping for 5 seconds"
                )
                time.sleep(5)
        self.log.fatal("instance data could not be fetched, quitting")
        sys.exit(1)

    def check_script(self, candidate):
        self.log.debug(f"found the file: {candidate}")
        if not os.path.isfile(candidate):
            self.log.warning(f"{candidate} is not a file")
            return None
        if not os.stat(candidate).st_uid == 0:
            self.log.warning("file not owned by root")
            return None
        if os.stat(candidate).st_mode & stat.S_IXUSR != 64:
            self.log.warning("file not executable by root")
            return None
        if os.stat(candidate).st_mode & stat.S_IWOTH == 2:
            self.log.warning("file group writeable")
            return None
        if os.stat(candidate).st_mode & stat.S_IWGRP == 16:
            self.log.warning("file world writeable")
            return None
        return True

    def get_scripts(self, path):
        files = list()
        candidates = os.listdir(path)
        candidates.sort()
        for candidate in candidates:
            candidate = os.path.join(path, candidate)
            if self.check_script(candidate=candidate):
                files.append(candidate)
        return files

    def run_scripts(self, script_type, failure_return_code=1):
        self.log.info(f"running {script_type} scripts")
        files = self.get_scripts(path=f"/etc/catweazle/{script_type}.d/")
        for _file in files:
            self.log.info(f"running: {_file}")
            if self._run_cmd([_file, self.fqdn, self.otp]) != 0:
                self.log.fatal("script failed, stopping!")
                sys.exit(failure_return_code)
            self.log.info(f"running: {_file} done")
        self.log.info(f"running {script_type} scripts, done")

    def run(self):
        self.log.info("Starting registration process")
        self.log.info(f"sleeping for {self.pre_sleep} seconds")
        time.sleep(self.pre_sleep)
        self.log.info(f"sleeping for {self.pre_sleep} seconds, done")
        self.log.info(f"instance-id is {self.instance_id}")
        self.get_cw_data()
        self.log.info(f"designated FQDN is {self.fqdn}")
        self.run_scripts(script_type="preflight", failure_return_code=0)
        self.run_scripts(script_type="register")
        self.log.info("Starting registration process, done")
