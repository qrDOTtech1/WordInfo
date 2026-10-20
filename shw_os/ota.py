# SHW OS - OTA Update Manager
# Pulls file manifest from server, downloads changed files to flash or SD
# Usage:
#   from ota import OTAUpdater
#   ota = OTAUpdater("http://YOUR_SERVER")
#   ota.check_and_apply()

import gc
import json
import os
import machine


try:
    import urequests as requests
except ImportError:
    import requests


VERSION_FILE = "ota_version.json"


class OTAUpdater:
    def __init__(self, server_url, reboot_on_update=True):
        self.server = server_url.rstrip("/")
        self.reboot = reboot_on_update
        self.version = self._read_version()

    def _read_version(self):
        try:
            with open(VERSION_FILE, "r") as f:
                return json.load(f).get("version", "0.0.0")
        except:
            return "0.0.0"

    def _save_version(self, ver):
        with open(VERSION_FILE, "w") as f:
            json.dump({"version": ver}, f)

    def _ensure_dir(self, path):
        parts = path.split("/")
        built = ""
        for p in parts[:-1]:
            built = built + "/" + p if built else p
            try:
                os.mkdir(built)
            except OSError:
                pass

    def check(self):
        try:
            r = requests.get(self.server + "/manifest.json")
            data = r.json()
            r.close()
            gc.collect()
            remote_ver = data.get("version", "0.0.0")
            if remote_ver != self.version:
                return data
            return None
        except Exception as e:
            print("[OTA] Check failed:", e)
            return None

    def _download_file(self, remote, local):
        self._ensure_dir(local)
        try:
            r = requests.get(self.server + "/" + remote)
            with open(local, "wb") as f:
                f.write(r.content)
            r.close()
            gc.collect()
            return True
        except Exception as e:
            print("[OTA] Download failed {}: {}".format(remote, e))
            return False

    def apply(self, manifest):
        files = manifest.get("files", [])
        ok = 0
        for entry in files:
            src = entry["src"]
            dst = entry["dst"]
            print("[OTA] {} -> {}".format(src, dst))
            if self._download_file(src, dst):
                ok += 1
        self._save_version(manifest["version"])
        print("[OTA] Updated to {} ({}/{} files)".format(
            manifest["version"], ok, len(files)
        ))
        if self.reboot and ok > 0:
            print("[OTA] Rebooting...")
            machine.reset()

    def check_and_apply(self):
        manifest = self.check()
        if manifest:
            print("[OTA] Update available: {} -> {}".format(
                self.version, manifest["version"]
            ))
            self.apply(manifest)
        else:
            print("[OTA] Up to date ({})".format(self.version))
