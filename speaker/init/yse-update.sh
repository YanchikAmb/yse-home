#!/bin/sh
# Раз в 3 минуты спрашивает сервер и забирает страницу, ystationd и будильник.
# Дом, ухо и музыка отсюда не перезапускаются.
DIR=/data/opt/yandex-station
echo $$ > "$DIR/yse-update.pid"
# Новый файл свежее процесса: перезапускаем только эту программу. Дом — тоже, если сменился housed.
need=0
for pair in alarmd:alarm.pid keys:keys.pid commandd:command.pid housed:house.pid; do
  bin=${pair%%:*}
  pidfile=${pair#*:}
  [ -f "$DIR/bin/$bin" ] && [ -f "$DIR/$pidfile" ] || continue
  pid=$(cat "$DIR/$pidfile" 2>/dev/null)
  if [ -n "$pid" ] && [ "$DIR/bin/$bin" -nt "/proc/$pid" ]; then
    kill "$pid" 2>/dev/null
    rm -f "$DIR/$pidfile"
    need=1
  fi
done
[ "$need" = 1 ] && /etc/init.d/S99house start
while true; do
  if [ -f "$DIR/yse-update.log" ] && [ "$(wc -c < "$DIR/yse-update.log")" -gt 40000 ]; then
    : > "$DIR/yse-update.log"
  fi
  host=$(sed -n 's/^host=//p' "$DIR/settings/host.conf" | tr -d '\r ')
  if [ -n "$host" ]; then
    list=$DIR/update.list
    if wget -q -T 15 -O "$list" "http://$host:18760/v1/update" 2>/dev/null \
      || curl -fsS -m 15 -o "$list" "http://$host:18760/v1/update" 2>/dev/null; then
      remote=$(sed -n 's/^version //p' "$list" | tr -d '\r ')
      local=$(tr -d '\r ' < "$DIR/update.version" 2>/dev/null)
      if [ -n "$remote" ] && [ "$remote" = "$local" ]; then
        :
      else
        page=0
        self=0
        alarm=0
        keys=0
        command=0
        house=0
        fail=0
        while read -r kind name sum mode tag; do
          [ "$kind" = file ] || continue
          dest="$DIR/$name"
          now=$(md5sum "$dest" 2>/dev/null | awk '{print $1}')
          [ "$now" = "$sum" ] && continue
          tmp="$dest.new"
          mkdir -p "${dest%/*}"
          if wget -q -T 20 -O "$tmp" "http://$host:18760/v1/update/file?name=$name" 2>/dev/null \
            || curl -fsS -m 20 -o "$tmp" "http://$host:18760/v1/update/file?name=$name" 2>/dev/null; then
            got=$(md5sum "$tmp" 2>/dev/null | awk '{print $1}')
            if [ "$got" = "$sum" ]; then
              chmod "$mode" "$tmp"
              mv "$tmp" "$dest"
              echo "updated $name"
              [ "$tag" = page ] && page=1
              [ "$tag" = self ] && self=1
              [ "$tag" = alarm ] && alarm=1
              [ "$tag" = keys ] && keys=1
              [ "$tag" = command ] && command=1
              [ "$tag" = house ] && house=1
            else
              rm -f "$tmp"
              fail=1
            fi
          else
            fail=1
          fi
        done < "$list"
        if [ "$fail" = 0 ] && [ -n "$remote" ]; then
          printf '%s\n' "$remote" > "$DIR/update.version"
        fi
        [ "$page" = 1 ] && /etc/init.d/S99ystation restart
        if [ "$alarm" = 1 ] || [ "$keys" = 1 ] || [ "$command" = 1 ] || [ "$house" = 1 ]; then
          if [ "$alarm" = 1 ] && [ -f "$DIR/alarm.pid" ]; then
            kill $(cat "$DIR/alarm.pid") 2>/dev/null
            rm -f "$DIR/alarm.pid"
          fi
          if [ "$keys" = 1 ] && [ -f "$DIR/keys.pid" ]; then
            kill $(cat "$DIR/keys.pid") 2>/dev/null
            rm -f "$DIR/keys.pid"
          fi
          if [ "$command" = 1 ] && [ -f "$DIR/command.pid" ]; then
            kill $(cat "$DIR/command.pid") 2>/dev/null
            rm -f "$DIR/command.pid"
          fi
          if [ "$house" = 1 ] && [ -f "$DIR/house.pid" ]; then
            kill $(cat "$DIR/house.pid") 2>/dev/null
            rm -f "$DIR/house.pid"
          fi
          /etc/init.d/S99house start
        fi
        [ "$self" = 1 ] && exec "$0"
      fi
    fi
  fi
  sleep 180
done
