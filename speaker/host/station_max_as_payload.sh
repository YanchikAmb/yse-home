#!/system/bin/sh
set +e
LOG=/data/local/tmp/autoroot_payload.log
: > "$LOG"
log() { echo "$1" >> "$LOG"; }
log "start id=$(id)"
mkdir -p /data/opt/bin /data/opt/etc/dropbear /data/opt/etc /etc
cat > /data/opt/etc/passwd <<'EOS'
root:x:0:0:root:/root:/system/bin/sh
EOS
cat > /data/opt/etc/shadow <<'EOS'
root::19000:0:99999:7:::
EOS
mkdir -p /etc
touch /etc/passwd /etc/shadow
mount -o bind /data/opt/etc/passwd /etc/passwd 2>> "$LOG"
mount -o bind /data/opt/etc/shadow /etc/shadow 2>> "$LOG"
log "passwd_mount:$(grep -c ^root: /etc/passwd 2>/dev/null)"
mkdir -p /data/opt/bin /data/opt/etc/dropbear
chmod 755 /data/local/tmp/dropbear /data/local/tmp/dropbearkey
cp /data/local/tmp/dropbear /data/local/tmp/dropbearkey /data/opt/bin/
chmod 755 /data/opt/bin/dropbear /data/opt/bin/dropbearkey
chcon u:object_r:system_file:s0 /data/opt/bin/dropbear /data/opt/bin/dropbearkey 2>> "$LOG"
DB=/data/opt/bin/dropbear
DK=/data/opt/bin/dropbearkey
HK=/data/opt/etc/dropbear/dropbear_ed25519_host_key
mkdir -p /data/opt/etc/dropbear
if [ -s /data/local/tmp/authorized_keys ]; then
  cp /data/local/tmp/authorized_keys /data/opt/etc/dropbear/authorized_keys
  chmod 600 /data/opt/etc/dropbear/authorized_keys
fi
"$DB" -h >> "$LOG" 2>&1
[ -s "$HK" ] || "$DK" -t ed25519 -f "$HK" >> "$LOG" 2>&1
log "hk_bytes=$(wc -c < "$HK" 2>/dev/null)"
pkill dropbear 2>/dev/null
"$DB" -B -D /data/opt/etc/dropbear -r "$HK" -p 2222 >> "$LOG" 2>&1 &
sleep 3
PID=$(pidof dropbear)
log "dropbear_pid=${PID:-none}"
chmod 666 /dev/graphics/fb4 /dev/graphics/fb5 2>> "$LOG"
chmod 666 /sys/class/backlight/fb4/brightness /sys/class/backlight/fb5/brightness 2>> "$LOG"
echo 200 > /sys/class/backlight/fb4/brightness 2>> "$LOG"
echo 255 > /sys/class/backlight/fb5/brightness 2>> "$LOG"
echo 0 > /sys/fs/selinux/enforce 2>> "$LOG"
log "enforce=$(getenforce 2>/dev/null)"
log "fb4=$(ls -l /dev/graphics/fb4 2>/dev/null)"
dmesg 2>/dev/null | grep -i avc | tail -n 5 >> "$LOG"
date > /dev/.yse_rooted
chmod 644 /dev/.yse_rooted
if [ -n "$PID" ]; then
  echo AUTOROOT_PAYLOAD_OK
else
  echo AUTOROOT_DROPBEAR_FAIL
  tail -n 12 "$LOG"
fi
id
getenforce
