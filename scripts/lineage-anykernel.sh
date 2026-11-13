### AnyKernel3 configuration for LineageOS on Redmi K60 / POCO F5 Pro.

properties() { '
kernel.string=LineageOS mondrian with Droidspaces
do.devicecheck=1
do.modules=0
do.systemless=0
do.cleanup=1
do.cleanuponabort=1
device.name1=mondrian
device.name2=23013RK75C
device.name3=Redmi K60
device.name4=POCO F5 Pro
supported.versions=
supported.patchlevels=
supported.vendorpatchlevels=
'; }

BLOCK=boot;
IS_SLOT_DEVICE=1;
RAMDISK_COMPRESSION=auto;
PATCH_VBMETA_FLAG=auto;

. tools/ak3-core.sh;

dump_boot;
write_boot;
