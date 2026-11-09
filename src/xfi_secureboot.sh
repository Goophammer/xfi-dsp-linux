#!/bin/bash
# xfi_secureboot.sh - Secure Boot support: sign our snd-ctxfi with DKMS's signing key and get that key trusted.
# UNTESTED on a Secure Boot system so far (written 2026-11-09 on a machine with Secure Boot off): please report.
#
# With Secure Boot on, the kernel only loads modules signed by a key it trusts. DKMS 3.x already signs every module it
# builds with its own key (Fedora/Nobara: /var/lib/dkms/mok.key + mok.pub, made on first use; Ubuntu: the shared
# /var/lib/shim-signed/mok/MOK.priv + MOK.der). That key only has to be ENROLLED once as a Machine Owner Key: mokutil
# queues it, and at the next boot a blue "MOK Manager" screen asks to confirm it (Enroll MOK, Continue, Yes, the
# one-time password chosen here, Reboot). From then on every module signed with it loads, also after kernel updates.
#
#   ./src/xfi_secureboot.sh status         Secure Boot on/off, the key, whether it is enrolled
#   sudo ./src/xfi_secureboot.sh sign KO   sign a module file with that key (no-op with Secure Boot off or if signed)
#   sudo ./src/xfi_secureboot.sh enroll    queue the key for enrollment at the next boot (asks for a one-time password)
# The installer and rearm.sh call these by themselves; nothing happens with Secure Boot off.
set -u

# XFI_SB_FORCE=1: act as if Secure Boot were on (testing); XFI_SB_KEY/XFI_SB_CERT: use another key pair
sb_on() { [ -n "${XFI_SB_FORCE:-}" ] || { command -v mokutil >/dev/null && mokutil --sb-state 2>/dev/null | grep -qi 'SecureBoot enabled'; }; }

# DKMS's key and certificate: its config, else its default
key_paths() {
	local f v
	KEY=/var/lib/dkms/mok.key CERT=/var/lib/dkms/mok.pub
	for f in /etc/dkms/framework.conf /etc/dkms/framework.conf.d/*.conf; do
		[ -r "$f" ] || continue
		v=$(sed -n 's/^[[:space:]]*mok_signing_key=["'\'']\{0,1\}\([^"'\''[:space:]]*\).*/\1/p' "$f" | tail -1); [ -n "$v" ] && KEY=$v
		v=$(sed -n 's/^[[:space:]]*mok_certificate=["'\'']\{0,1\}\([^"'\''[:space:]]*\).*/\1/p' "$f" | tail -1); [ -n "$v" ] && CERT=$v
	done
	KEY=${XFI_SB_KEY:-$KEY} CERT=${XFI_SB_CERT:-$CERT}
}

# made the way DKMS makes it, so DKMS uses the same key later
make_key() {
	[ -f "$KEY" ] && [ -f "$CERT" ] && return 0
	command -v openssl >/dev/null || { echo "openssl is needed to create a signing key"; return 1; }
	echo "== creating a module signing key ($KEY, $CERT)"
	mkdir -p "$(dirname "$KEY")" "$(dirname "$CERT")"
	openssl req -new -x509 -nodes -days 36500 -subj "/CN=DKMS module signing key" -newkey rsa:2048 \
		-keyout "$KEY" -outform DER -out "$CERT" 2>/dev/null || { echo "could not create the key"; return 1; }
	chmod 600 "$KEY"
}

enrolled() { mokutil --test-key "$CERT" 2>/dev/null | grep -q 'is already enrolled'; }
pending() { mokutil --list-new 2>/dev/null | grep -qiE 'certificate|fingerprint'; }

key_paths
case "${1:-}" in
status)
	if sb_on; then echo "Secure Boot: on"; else echo "Secure Boot: off (or mokutil missing) - nothing to do"; fi
	echo "signing key: $KEY"; echo "certificate: $CERT"
	if [ ! -f "$CERT" ]; then echo "certificate: not created yet"
	elif enrolled; then echo "enrolled: yes"
	elif pending; then echo "enrolled: queued, confirm it in the blue MOK Manager screen at the next boot"
	else echo "enrolled: no"; fi
	;;
sign)
	KO=${2:-}
	[ -f "$KO" ] || { echo "usage: sudo $0 sign MODULE.ko"; exit 1; }
	sb_on || exit 0
	[ -n "$(modinfo -F signer "$KO" 2>/dev/null)" ] && exit 0	# signing twice would break the signature
	make_key || exit 1
	SF=/lib/modules/$(uname -r)/build/scripts/sign-file
	[ -x "$SF" ] || { echo "$SF missing (kernel headers for $(uname -r) installed?)"; exit 1; }
	HASH=$(sed -n 's/^CONFIG_MODULE_SIG_HASH="\(.*\)"/\1/p' "/lib/modules/$(uname -r)/build/.config" 2>/dev/null)
	"$SF" "${HASH:-sha256}" "$KEY" "$CERT" "$KO" || { echo "signing $KO failed"; exit 1; }
	echo "signed $KO ($(modinfo -F signer "$KO"))"
	;;
enroll)
	sb_on || { echo "Secure Boot is off - no key needs enrolling"; exit 0; }
	[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
	make_key || exit 1
	if enrolled; then echo "the module signing key is already enrolled"; exit 0; fi
	if pending; then
		echo "A key is already queued for enrollment: confirm it in the blue MOK Manager screen at the next boot."
		exit 0
	fi
	echo
	echo "== Secure Boot is on: the driver's signing key has to be enrolled once."
	echo "   Choose a one-time password now (you type it once more at the next boot, nothing else uses it)."
	mokutil --import "$CERT" || { echo "mokutil --import failed"; exit 1; }
	echo
	echo "   At the next boot a blue 'MOK Manager' screen appears (it waits only 10 seconds, press a key):"
	echo "   Enroll MOK -> Continue -> Yes -> the password -> Reboot. Until then the driver can't load."
	;;
*)
	sed -n '2,/^set -u/p' "$0" | sed '$d'; exit 1 ;;
esac
