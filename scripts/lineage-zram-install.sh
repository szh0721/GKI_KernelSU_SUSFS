# Sourced by anykernel.sh after unpacking vendor_boot. No partition writes here.
install_lineage_zram() {
  local root module_dir found old_hash marker;
  found=0;
  module_dir="";
  for root in "$RAMDISK" "$VENDORRD"/*; do
    if [ -f "$root/lib/modules/zram.ko" ]; then
      found=$((found + 1));
      module_dir="$root/lib/modules";
    fi;
  done;
  [ "$found" -eq 1 ] || abort "Expected exactly one ZRAM module in vendor_boot.";
  [ -f "$module_dir/modules.load" ] || abort "Missing vendor_boot modules.load.";
  [ -f "$module_dir/modules.dep" ] || abort "Missing vendor_boot modules.dep.";
  [ "$(sha256sum "$module_dir/zsmalloc.ko" | cut -d' ' -f1)" = "@STOCK_ZSMALLOC_SHA256@" ] ||
    abort "The installed zsmalloc module differs from this ROM reference.";
  grep -qx '/lib/modules/zram.ko: /lib/modules/zsmalloc.ko' "$module_dir/modules.dep" ||
    abort "Unexpected ZRAM dependencies; refusing to change the ramdisk.";
  [ "$(sha256sum "$AKHOME/lineage-zram/zram.ko" | cut -d' ' -f1)" = "@NEW_ZRAM_SHA256@" ] ||
    abort "Replacement ZRAM checksum mismatch.";
  old_hash=$(sha256sum "$module_dir/zram.ko" | cut -d' ' -f1);
  marker="$module_dir/lineage-zram-original.sha256";
  if [ "$old_hash" != "@STOCK_ZRAM_SHA256@" ]; then
    [ -f "$marker" ] && [ "$(cat "$marker")" = "@STOCK_ZRAM_SHA256@" ] ||
      abort "The vendor_boot ZRAM module is not the expected stock or previously installed driver.";
  fi;
  cp -f "$AKHOME/lineage-zram/zram.ko" "$module_dir/zram.ko" ||
    abort "Could not install the replacement ZRAM module.";
  printf '%s\n' "@STOCK_ZRAM_SHA256@" > "$marker";
  if ! grep -Eq '^(zram|zram\.ko|/lib/modules/zram\.ko)$' "$module_dir/modules.load"; then
    printf '\nzram\n' >> "$module_dir/modules.load";
  fi;
  set_perm 0 0 644 "$module_dir/zram.ko" "$marker" "$module_dir/modules.load";
  ui_print "ZRAM algorithm list updated; driver will load from vendor_boot.";
}

install_lineage_zram;
