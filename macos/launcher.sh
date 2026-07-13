#!/bin/bash

set -u

case "$0" in
    */*) SCRIPT_DIR=${0%/*} ;;
    *) SCRIPT_DIR=. ;;
esac
if ! CDPATH= cd -P -- "$SCRIPT_DIR" 2>/dev/null; then
    printf '%s\n' 'ランチャーの場所を確認できませんでした。'
    exit 1
fi
RESOURCES=$PWD
PYTHON_EXECUTABLE=

exec 3< "$RESOURCES/python-path"
IFS= read -r PYTHON_EXECUTABLE <&3 || {
    exec 3<&-
    printf '%s\n' 'Pythonの固定パスを読み込めませんでした。'
    exit 1
}
_EXTRA_LINE=
if IFS= read -r _EXTRA_LINE <&3 || [ -n "$_EXTRA_LINE" ]; then
    exec 3<&-
    printf '%s\n' 'Pythonの固定パスが不正です。'
    exit 1
fi
exec 3<&-

case "$PYTHON_EXECUTABLE" in
    /*) ;;
    *) printf '%s\n' 'Pythonの固定パスが不正です。'; exit 1 ;;
esac
case "$PYTHON_EXECUTABLE" in
    *[$'\001'-$'\037'$'\177']*)
        printf '%s\n' 'Pythonの固定パスが不正です。'
        exit 1
        ;;
esac

PYTHONDONTWRITEBYTECODE=1 "$PYTHON_EXECUTABLE" -B "$RESOURCES/runtime.py"
status=$?
if [ "$status" -eq 0 ]; then
    printf '%s\n' '処理が正常に完了しました。'
else
    printf '処理に失敗しました（終了コード: %s）。\n' "$status"
fi
if [ -t 0 ]; then
    printf '%s' 'Enterキーを押して閉じてください。'
    IFS= read -r _WAIT || true
fi
exit "$status"
