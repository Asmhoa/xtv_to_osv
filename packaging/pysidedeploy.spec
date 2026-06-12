[app]

title = XtraToOsmo
project_dir = ..
input_file = ../app.py
exec_directory = ../dist
project_file =
icon =

[python]

python_path =
packages = Nuitka==4.0,zstandard==0.23.0
android_packages = buildozer==1.5.0,cython==0.29.33

[qt]

qml_files =
excluded_qml_plugins =
modules = Core,Gui,Widgets
plugins = platforms,styles,iconengines,imageformats,platforminputcontexts

[android]

wheel_pyside =
wheel_shiboken =
plugins =

[nuitka]

macos.permissions =
mode = onefile
extra_args = --quiet --assume-yes-for-downloads --noinclude-qt-translations

[buildozer]

mode = debug
recipe_dir =
jars_dir =
ndk_path =
sdk_path =
local_libs =
arch =
