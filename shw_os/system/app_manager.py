# SHW OS - App Manager
# Scans /sd/apps for installed apps, loads app manifests
import ujson
import uos


class AppInfo:
    def __init__(self, name, path, icon=None, version="1.0"):
        self.name = name
        self.path = path
        self.icon = icon
        self.version = version


class AppManager:
    def __init__(self, sd):
        self.sd = sd
        self.apps = []

    def scan(self):
        self.apps = []
        if not self.sd.mounted:
            return
        app_dirs = self.sd.list_dir("/sd/apps")
        for d in app_dirs:
            manifest_path = "/sd/apps/{}/manifest.json".format(d)
            try:
                with open(manifest_path, "r") as f:
                    m = ujson.load(f)
                self.apps.append(AppInfo(
                    name=m.get("name", d),
                    path="/sd/apps/{}".format(d),
                    icon=m.get("icon"),
                    version=m.get("version", "1.0"),
                ))
            except:
                # No manifest = use folder name
                self.apps.append(AppInfo(name=d, path="/sd/apps/{}".format(d)))
        print("[AppManager] Found {} apps".format(len(self.apps)))

    def launch(self, app_info, hal):
        try:
            mod_path = "{}/main".format(app_info.path).replace("/sd/", "").replace("/", ".")
            mod = __import__(mod_path)
            if hasattr(mod, "run"):
                mod.run(hal)
        except Exception as e:
            print("[AppManager] Error launching {}: {}".format(app_info.name, e))
