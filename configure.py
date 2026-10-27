"""Offline, fail-closed configuration preparation. Never edits core or sends QQ messages."""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import hashlib
import importlib
import json
import os
import shutil
import sys
import tempfile
import tomllib
import types

HERE = Path(__file__).resolve().parent
PACKAGE = '_persona_configuration'
if PACKAGE not in sys.modules:
    pkg = types.ModuleType(PACKAGE)
    pkg.__path__ = [str(HERE)]
    sys.modules[PACKAGE] = pkg
Router = importlib.import_module(PACKAGE + '.routing').Router
qq_id = importlib.import_module(PACKAGE + '.routing').qq_id
PrimaryBoundary = importlib.import_module(PACKAGE + '.boundary').PrimaryBoundary


def build_plan(root, plugin_dir, profiles, authorized=(), *, unlock=False, emoji_for=()):
    root, plugin_dir = Path(root).resolve(), Path(plugin_dir)
    if plugin_dir.is_symlink() or plugin_dir.resolve().parent != root / 'plugins':
        raise ValueError('Plugin must be a real directory directly under the selected MaiBot plugins directory')
    plugin_dir = plugin_dir.resolve()
    if not plugin_dir.is_dir():
        raise ValueError('Clone the plugin directory first')
    policy_path = root / 'config/adapter_policy.toml'
    policy_raw = policy_path.read_bytes()
    policy = tomllib.loads(policy_raw.decode('utf-8'))
    group = policy['defaults'].get('group', {})
    if group.get('default_action') != 'block' or group.get('allow_ids'):
        raise ValueError('This release supports private-only deployments with group chats blocked')
    legacy = policy['defaults']['private']['allow_ids']
    if not isinstance(legacy, list) or not legacy:
        raise ValueError('An existing explicit core private allowlist is required')
    bot = tomllib.loads((root / 'config/bot_config.toml').read_text(encoding='utf-8'))
    account = qq_id(str(bot['bot']['qq_account']))
    router = Router.build(account, legacy, profiles)
    authorized = frozenset(qq_id(x) for x in authorized)
    emoji_for = frozenset(qq_id(x) for x in emoji_for)
    if authorized.intersection(router.legacy_ids) or not authorized.issubset(router.personas):
        raise ValueError('Only configured secondary profiles may be authorized')
    if any(not router.personas[x].enabled for x in authorized):
        raise ValueError('An authorized persona must have enabled=true in the private profile file')
    if unlock and not authorized:
        raise ValueError('Unlock requires at least one explicit secondary authorization')
    if not emoji_for.issubset(authorized):
        raise ValueError('Emoji users must already be explicitly authorized')
    adapter = tomllib.loads((root / 'plugins/MaiBot-SnowLuma-Adapter/config.toml').read_text(encoding='utf-8'))
    filters = adapter['filters']
    if {filters['private_owner_id'], *filters.get('private_allowed_ids', [])} != set(router.legacy_ids):
        raise ValueError('Original adapter filters must exactly match the original core allowlist')
    policy_hash = hashlib.sha256(policy_raw).hexdigest()
    filters_hash = hashlib.sha256(json.dumps(filters, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    PrimaryBoundary(root, router, authorized, policy_hash, filters_hash).check()
    # Validates only the audited reference endpoint/provider. No network I/O.
    importlib.import_module(PACKAGE + '.transport').Credentials.read(root)
    emoji = json.loads((HERE / 'emoji_policy.example.json').read_text(encoding='utf-8'))
    if emoji_for:
        helper_root = root / 'plugins/togawasakiko-desuwa_emoji-select-with-text'
        if helper_root.is_symlink():
            raise ValueError('Unsafe selector directory')
        for name, digest in emoji['source_hashes'].items():
            path = helper_root / name
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError('Emoji selector differs from the audited version; review compatibility before enabling')
    emoji['users'] = {uid: dict(enabled=True, auto_probability=.2, cooldown_seconds=90, selection_timeout=15)
                      for uid in sorted(emoji_for)}
    replies = dict(schema_version=1, users={uid: dict(enabled=True, temperature=.65, max_tokens=256,
                   assistant_context_after=0, allow_ack_silence=True) for uid in sorted(authorized)})
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    cfg = ('[plugin]\nconfig_version = "0.2.3"\nenabled = false\n\n[router]\n'
           f'account_id = {quote(account)}\nlegacy_user_ids = {quote(sorted(router.legacy_ids))}\n'
           f'authorized_user_ids = {quote(sorted(authorized))}\n'
           f'secondary_privacy_lock = {str(not unlock).lower()}\n'
           f'primary_policy_sha256 = "{policy_hash}"\nadapter_filters_sha256 = "{filters_hash}"\n')
    encode = lambda value: json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    return {'config.toml': cfg, 'personas.json': encode(profiles),
            'reply_policy.json': encode(replies), 'emoji_policy.json': encode(emoji)}


def write_plan(root, plugin_dir, plan, *, replace=False):
    root, plugin_dir = Path(root).resolve(), Path(plugin_dir).resolve()
    if plugin_dir.parent != root / 'plugins' or set(plan) != {'config.toml','personas.json','reply_policy.json','emoji_policy.json'}:
        raise ValueError('Unexpected configuration destination')
    targets = [plugin_dir / name for name in plan]
    if any(path.is_symlink() for path in targets):
        raise ValueError('Refusing symlinked configuration')
    existing = [path for path in targets if path.exists()]
    if existing and not replace:
        raise ValueError('Existing private configuration found; stop the plugin and use --replace only after reviewing changes')
    backup = None
    if existing:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
        backup = root / 'data/plugins/local.per-user-persona/config-backups' / stamp
        backup.mkdir(mode=0o700, parents=True, exist_ok=False)
        for path in existing:
            saved = backup / path.name
            shutil.copyfile(path, saved)
            saved.chmod(0o600)
    # Disable first. A partial write must never silently enable a persona.
    for name in ['config.toml','personas.json','reply_policy.json','emoji_policy.json']:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n', dir=plugin_dir,
                                         prefix='.' + name + '.', suffix='.tmp', delete=False) as stream:
            stream.write(plan[name])
            temp = Path(stream.name)
        temp.chmod(0o600)
        os.replace(temp, plugin_dir / name)
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True, help='Existing MaiBot project root as seen by this process')
    parser.add_argument('--profiles', type=Path, required=True, help='Private profile JSON; see personas.example.json')
    parser.add_argument('--authorize', action='append', default=[], help='One secondary QQ ID; repeat for more users')
    parser.add_argument('--unlock', action='store_true', help='Prepare unlocked secondary routing; plugin remains disabled')
    parser.add_argument('--emoji-for', action='append', default=[], help='Optional authorized secondary QQ ID to enable emoji for')
    parser.add_argument('--write', action='store_true', help='Write only this plugin private config after successful validation')
    parser.add_argument('--replace', action='store_true', help='Back up and replace existing private config; stop plugin first')
    args = parser.parse_args()
    try:
        profiles = json.loads(args.profiles.read_text(encoding='utf-8'))
        plan = build_plan(args.root, HERE, profiles, args.authorize, unlock=args.unlock, emoji_for=args.emoji_for)
        backup = write_plan(args.root, HERE, plan, replace=args.replace) if args.write else None
        print(json.dumps(dict(valid=True, written=args.write, plugin_enabled=False,
                              secondary_count=len(set(args.authorize)), emoji_count=len(set(args.emoji_for)),
                              backup_created=backup is not None)))
    except Exception as exc:
        # No profile text, IDs, paths or credential values in diagnostics.
        print(json.dumps(dict(valid=False, error=type(exc).__name__,
                              hint='Check documented prerequisites, profile validity, existing host history, and whether files already exist.')))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
