# SHW OS - OTA Update Manager
# Downloads updates from a server over WiFi and applies them
import gc
import urequests
import ujson
import uos


OTA_SERVER = "http://YOUR_SERVER/ota"


class OTAUpdater:
    def __init__(self, sd):
        self.sd = sd
        self.current_version = self._read_version()

    def _read_version(self):
        try:
            with open("/sd/config/version.json", "r") as f:
                return ujson.load(f).get("version", "0.0.0")
        except:
            return "0.0.0"

    def _save_version(self, ver):
        with open("/sd/config/version.json", "w") as f:
            ujson.dump({"version": ver}, f)

    def check_update(self):
        try:
            r = urequests.get("{}/manifest.json".format(OTA_SERVER))
            manifest = r.json()
            r.close()
            if manifest.get("version") != self.current_version:
                return manifest
            return None
        except Exception as e:
            print("[OTA] Check failed:", e)
            return None

    def download_file(self, remote_path, local_path):
        try:
            r = urequests.get("{}/{}".format(OTA_SERVER, remote_path))
            with open(local_path, "wb") as f:
                f.write(r.content)
            r.close()
            gc.collect()
            return True
        except Exception as e:
            print("[OTA] Download failed:", e)
            return False

    def apply_update(self, manifest):
        files = manifest.get("files", [])
        for finfo in files:
            src = finfo["src"]
            dst = finfo["dst"]
            # Ensure parent dir exists
            parts = dst.rsplit("/", 1)
            if len(parts) > 1:
                try:
                    uos.mkdir(parts[0])
                except OSError:
                    pass
            print("[OTA] Updating:", dst)
            if not self.download_file(src, dst):
                return False
        self._save_version(manifest["version"])
        print("[OTA] Update complete:", manifest["version"])
        return True
