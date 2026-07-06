_vd_names()
{
    local vd_list_file="${VD_LIST_FILE:-/etc/dnlab-vds}"
    if [ -n "${JUMPHOST_VD_LIST:-}" ]; then
        local -a names
        read -r -a names <<< "$JUMPHOST_VD_LIST"
        printf '%s\n' "${names[@]}"
    elif [ -f "$vd_list_file" ]; then
        local item
        while IFS= read -r item; do
            [ -n "$item" ] || continue
            printf '%s\n' "${item%%=*}"
        done < "$vd_list_file"
    fi
}

_vd()
{
    local cur cmd names
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    cmd="${COMP_WORDS[1]:-}"

    if [ "$COMP_CWORD" -eq 1 ]; then
        COMPREPLY=( $(compgen -W "list connect log help" -- "$cur") )
        return 0
    fi

    case "$cmd" in
        connect)
            if [ "$COMP_CWORD" -eq 2 ]; then
                names="$(_vd_names)"
                COMPREPLY=( $(compgen -W "$names" -- "$cur") )
            fi
            ;;
        log)
            names="$(_vd_names)"
            if [ "$COMP_CWORD" -eq 2 ]; then
                COMPREPLY=( $(compgen -W "-f $names" -- "$cur") )
            elif [ "${COMP_WORDS[2]:-}" = "-f" ] && [ "$COMP_CWORD" -eq 3 ]; then
                COMPREPLY=( $(compgen -W "$names" -- "$cur") )
            fi
            ;;
    esac
}

complete -F _vd vd
