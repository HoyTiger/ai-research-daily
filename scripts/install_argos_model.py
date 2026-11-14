#!/usr/bin/env python3
"""Install the English-to-Chinese Argos model used by the sync workflow."""

from argostranslate import package


def main() -> int:
    installed = package.get_installed_packages()
    if any(
        item.from_code == "en" and item.to_code == "zh" for item in installed
    ):
        print("English -> Chinese Argos model already installed.")
        return 0

    package.update_package_index()
    candidates = [
        item
        for item in package.get_available_packages()
        if item.from_code == "en" and item.to_code == "zh"
    ]
    if not candidates:
        raise RuntimeError("No English -> Chinese Argos model is available.")

    model = sorted(candidates, key=lambda item: item.package_version)[-1]
    print(
        f"Installing {model.from_name} -> {model.to_name} "
        f"v{model.package_version}..."
    )
    package.install_from_path(model.download())
    print("Installed English -> Chinese Argos model.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
