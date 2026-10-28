"""Build dist/tokendiet-<version>.vsix without Node: a .vsix is a zip with a manifest and content types.

Bundles the Python package under extension/python so the extension can pip-install it locally.

  python vscode/build_vsix.py
"""

import json
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PKG = json.loads((HERE / "package.json").read_text(encoding="utf-8"))

CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension=".json" ContentType="application/json"/><Default Extension=".js" ContentType="application/javascript"/>
<Default Extension=".md" ContentType="text/markdown"/><Default Extension=".py" ContentType="text/plain"/>
<Default Extension=".toml" ContentType="text/plain"/><Default Extension=".txt" ContentType="text/plain"/>
<Default Extension=".vsixmanifest" ContentType="text/xml"/>
</Types>"""

MANIFEST = f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011" xmlns:d="http://schemas.microsoft.com/developer/vsx-schema-design/2011">
  <Metadata>
    <Identity Language="en-US" Id="{PKG['name']}" Version="{PKG['version']}" Publisher="{PKG['publisher']}" />
    <DisplayName>{PKG['displayName']}</DisplayName>
    <Description xml:space="preserve">{PKG['description']}</Description>
    <Tags>{','.join(PKG['keywords'])}</Tags>
    <Categories>{','.join(PKG['categories'])}</Categories>
    <GalleryFlags>Public</GalleryFlags>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="{PKG['engines']['vscode']}" />
      <Property Id="Microsoft.VisualStudio.Code.ExtensionDependencies" Value="" />
      <Property Id="Microsoft.VisualStudio.Code.ExtensionPack" Value="" />
      <Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="workspace" />
    </Properties>
  </Metadata>
  <Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true" />
    <Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md" Addressable="true" />
    <Asset Type="Microsoft.VisualStudio.Services.Content.License" Path="extension/LICENSE.txt" Addressable="true" />
  </Assets>
</PackageManifest>"""


def main():
    out = ROOT / "dist" / f"{PKG['name']}-{PKG['version']}.vsix"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("extension.vsixmanifest", MANIFEST)
        for name in ("package.json", "extension.js", "README.md"):
            z.write(HERE / name, f"extension/{name}")
        z.write(ROOT / "LICENSE", "extension/LICENSE.txt")
        z.write(ROOT / "LICENSE", "extension/python/LICENSE")
        z.write(ROOT / "pyproject.toml", "extension/python/pyproject.toml")
        z.write(ROOT / "README.md", "extension/python/README.md")
        for f in sorted((ROOT / "tokendiet").glob("*.py")):
            z.write(f, f"extension/python/tokendiet/{f.name}")
    print(out, f"{out.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
