#!/usr/bin/env python3
# meiva:link ui/app/docs/app.meiva.okf.md

from __future__ import annotations

import argparse
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ANDROID_NS = "http://schemas.android.com/apk/res/android"
ET.register_namespace("android", ANDROID_NS)

#: Java package the app is generated into — derived from `identifier` in
#: tauri.conf.json, and hard-coded in the Kotlin sources' `package` line.
PACKAGE = "com.meiva.app"

PERMISSIONS = (
    "android.permission.POST_NOTIFICATIONS",
    "android.permission.REQUEST_INSTALL_PACKAGES",
)

_GRADLE_SIGNING_MARKER = "// Meiva release signing overlay."


def _android(attr: str) -> str:
    """Attribute name in the Android namespace, ElementTree's `{ns}name` form."""
    return f"{{{ANDROID_NS}}}{attr}"


def patch_manifest(manifest_xml: str) -> str:
    # Input is Tauri's locally generated build file, never network/user XML.
    root = ET.fromstring(manifest_xml)  # noqa: S314
    if root.tag != "manifest":
        raise ValueError(f"expected a <manifest> root, found <{root.tag}>")
    application = root.find("application")
    if application is None:
        raise ValueError("the manifest has no <application> element to patch")

    existing = {element.get(_android("name")) for element in root.findall("uses-permission")}
    for permission in PERMISSIONS:
        if permission in existing:
            continue
        # Permissions belong before <application>; ElementTree keeps document
        # order, so insert rather than append.
        element = ET.Element("uses-permission", {_android("name"): permission})
        root.insert(len(root.findall("uses-permission")) + 1, element)

    application.set(_android("networkSecurityConfig"), "@xml/network_security_config")
    # The network security config is the finer-grained statement; leaving the
    # blanket flag next to it would just be a second, contradictory answer.
    application.attrib.pop(_android("usesCleartextTraffic"), None)

    ET.indent(root, space="    ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


_MAIN_ACTIVITY_MARKER = "// Meiva system-bar insets overlay."

# meiva:link ui/app/docs/android-shell-chrome.meiva.okf.md
_MAIN_ACTIVITY_INSETS = """
    {marker}
    val meivaContent = findViewById<View>(android.R.id.content)
    ViewCompat.setOnApplyWindowInsetsListener(meivaContent) { view, insets ->
      val bars = insets.getInsets(
        WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout()
      )
      view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
      insets
    }
    ViewCompat.requestApplyInsets(meivaContent)
"""

_MAIN_ACTIVITY_IMPORTS = (
    "import android.view.View\n"
    "import androidx.core.view.ViewCompat\n"
    "import androidx.core.view.WindowInsetsCompat\n"
)


def patch_main_activity(source: str) -> str:
    if _MAIN_ACTIVITY_MARKER in source:
        return source
    anchor = "super.onCreate(savedInstanceState)"
    if anchor not in source:
        raise ValueError(f"unrecognised Tauri MainActivity template; missing {anchor!r}")
    if "import android.os.Bundle" not in source:
        raise ValueError("unrecognised Tauri MainActivity template; no Bundle import")
    patched = source.replace(
        "import android.os.Bundle\n",
        "import android.os.Bundle\n" + _MAIN_ACTIVITY_IMPORTS,
        1,
    )
    return patched.replace(
        anchor + "\n",
        anchor + "\n" + _MAIN_ACTIVITY_INSETS.replace("{marker}", _MAIN_ACTIVITY_MARKER),
        1,
    )


def patch_gradle(build_gradle: str) -> str:
    """Wire Tauri's generated Android app to an optional CI keystore.

    Tauri intentionally leaves release signing out of its generated Gradle
    file; merely writing ``keystore.properties`` does nothing. This patch is
    the documented Tauri 2 signing integration, guarded by file existence so a
    checkout with no signing secrets still produces an unsigned APK.
    """
    if _GRADLE_SIGNING_MARKER in build_gradle:
        return build_gradle
    required = ("import java.util.Properties", "    buildTypes {", 'getByName("release") {')
    missing = [needle for needle in required if needle not in build_gradle]
    if missing:
        raise ValueError(f"unrecognised Tauri Gradle template; missing {missing}")

    patched = build_gradle.replace(
        "import java.util.Properties",
        "import java.io.FileInputStream\nimport java.util.Properties",
        1,
    )
    signing = f"""    {_GRADLE_SIGNING_MARKER}
    signingConfigs {{
        create("release") {{
            val keystorePropertiesFile = rootProject.file("keystore.properties")
            if (keystorePropertiesFile.exists()) {{
                val keystoreProperties = Properties().apply {{
                    load(FileInputStream(keystorePropertiesFile))
                }}
                keyAlias = keystoreProperties["keyAlias"] as String
                keyPassword = keystoreProperties["password"] as String
                storeFile = file(keystoreProperties["storeFile"] as String)
                storePassword = keystoreProperties["password"] as String
            }}
        }}
    }}

"""
    patched = patched.replace("    buildTypes {", signing + "    buildTypes {", 1)
    return patched.replace(
        '        getByName("release") {',
        """        getByName("release") {
            if (rootProject.file("keystore.properties").exists()) {
                signingConfig = signingConfigs.getByName("release")
            }""",
        1,
    )


def apply(overlay_dir: Path, gen_dir: Path) -> None:
    """Copy the Kotlin, resource, and ProGuard overlay into `gen_dir`."""
    app_dir = gen_dir / "app"
    main = app_dir / "src" / "main"
    if not main.is_dir():
        raise SystemExit(
            f"{main} does not exist — run `cargo tauri android init` before this script"
        )

    package_dir = main / "java" / Path(*PACKAGE.split("."))
    package_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted((overlay_dir / "java").glob("*.kt")):
        shutil.copy2(source, package_dir / source.name)
    shutil.copy2(overlay_dir / "proguard-meiva.pro", app_dir / "proguard-meiva.pro")

    xml_dir = main / "res" / "xml"
    xml_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        overlay_dir / "network_security_config.xml",
        xml_dir / "network_security_config.xml",
    )

    manifest = main / "AndroidManifest.xml"
    manifest.write_text(patch_manifest(manifest.read_text(encoding="utf-8")) + "\n")

    gradle = app_dir / "build.gradle.kts"
    gradle.write_text(patch_gradle(gradle.read_text(encoding="utf-8")), encoding="utf-8")

    main_activity = package_dir / "MainActivity.kt"
    if not main_activity.is_file():
        raise SystemExit(f"{main_activity} does not exist — the Tauri template changed")
    main_activity.write_text(
        patch_main_activity(main_activity.read_text(encoding="utf-8")), encoding="utf-8"
    )


def main(argv: list[str]) -> int:
    overlay_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gen-dir",
        type=Path,
        default=overlay_dir.parent / "src-tauri" / "gen" / "android",
        help="the generated Android project (default: ui/app/src-tauri/gen/android)",
    )
    args = parser.parse_args(argv)
    apply(overlay_dir, args.gen_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
