#!/bin/bash
set -u

case "$0" in
    */*) SCRIPT_PATH=$0 ;;
    *) printf '%s\n' 'インストーラーの場所を確認できませんでした。'; exit 1 ;;
esac
if [ -L "$SCRIPT_PATH" ] || [ ! -f "$SCRIPT_PATH" ]; then
    printf '%s\n' 'インストーラーの場所を確認できませんでした。'
    exit 1
fi
SCRIPT_DIR=${SCRIPT_PATH%/*}
if ! CDPATH= cd -P -- "$SCRIPT_DIR"; then
    printf '%s\n' 'インストーラーの場所を確認できませんでした。'
    exit 1
fi
SOURCE_ROOT=${PWD%/macos}

trusted_python_candidate() {
    candidate_path=$1
    candidate_metadata=$(/usr/bin/stat -f '%u:%g:%Lp:%HT' -- "$candidate_path") || return 1
    candidate_uid=$(/usr/bin/id -u) || return 1
    candidate_groups=$(/usr/bin/id -G) || return 1
    case "$candidate_metadata" in
        *'
'*) return 1 ;;
    esac
    old_ifs=$IFS
    IFS=:
    read -r file_uid file_gid file_mode file_type <<EOF
$candidate_metadata
EOF
    IFS=$old_ifs
    case "$file_uid" in *[!0-9]*|'') return 1 ;; esac
    case "$file_gid" in *[!0-9]*|'') return 1 ;; esac
    case "$candidate_uid" in *[!0-9]*|'') return 1 ;; esac
    [ "$file_type" = "Regular File" ] || return 1
    [ "$file_uid" = 0 ] || [ "$file_uid" = "$candidate_uid" ] || return 1
    case "$file_mode" in
        [0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) ;;
        *) return 1 ;;
    esac
    mode_value=$((8#$file_mode))
    [ $((mode_value & 3072)) -eq 0 ] || return 1
    [ $((mode_value & 2)) -eq 0 ] || return 1
    if [ $((mode_value & 16)) -ne 0 ]; then
        for member_gid in $candidate_groups; do
            case "$member_gid" in *[!0-9]*|'') return 1 ;; esac
            [ "$member_gid" = "$file_gid" ] && return 1
        done
    fi
    return 0
}

for candidate in \
    /Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14 \
    /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
    /opt/homebrew/bin/python3.14 \
    /usr/local/bin/python3.14 \
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 \
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
    /opt/homebrew/bin/python3.13 \
    /usr/local/bin/python3.13
do
    if [ -f "$candidate" ] && [ ! -L "$candidate" ] &&
       trusted_python_candidate "$candidate" &&
       "$candidate" -I -c 'import sys; raise SystemExit(0 if sys.version_info[:2] in {(3,13),(3,14)} else 1)'
    then
        if [ "$#" -eq 1 ] && [ "$1" = "--help" ]; then
            exec "$candidate" -I -c 'import runpy,sys; root=sys.argv.pop(1); sys.path.insert(0,root); runpy.run_module("macos.install_app",run_name="__main__")' "$SOURCE_ROOT" --help
        fi
        exec "$candidate" -I -c 'import runpy,sys; root=sys.argv.pop(1); sys.path.insert(0,root); runpy.run_module("macos.install_app",run_name="__main__")' "$SOURCE_ROOT" --source-root "$SOURCE_ROOT"
    fi
done

printf '%s\n' 'Python 3.13 または 3.14 が必要です。Python公式installerまたはHomebrewで導入してください。'
if [ -t 0 ]; then
    printf '%s' 'Enterキーを押して閉じてください。'
    IFS= read -r _WAIT || true
fi
exit 1
