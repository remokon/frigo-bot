"""Export Showdown replay logs as compact, ordered JSON action streams.

Examples:
    uv run python extract_battle_actions.py --frigo 2258 2259
    uv run python extract_battle_actions.py --frigo all
    uv run python extract_battle_actions.py --replay-link https://replay.pokemonshowdown.com/gen9freeforallrandombattle-123

Each action has the same fields: sequence, category, event, timestamp, turn,
player, player_id, pokemon, target_player, target_player_id, target_pokemon,
move, message, details, and raw. Values that do not apply are null (or an
empty details list). Raw protocol lines are retained for lossless reference.
Move actions additionally contain an ordered ``effects`` list with the protocol
outcomes caused by that move, plus exact ``damage_amount`` values when HP
fractions make them calculable, ``damage``, ``critical_hit``, and ``missed``
summary fields. Effect raw lines are nested under their move instead of being
exported as separate actions.
"""
import argparse
import json
import re
import sqlite3
from pathlib import Path

import requests

import replay_reader

DEFAULT_DB_PATH = 'frigo.db'
DEFAULT_OUTPUT_DIR = Path('resources/battle_actions')
ENTITY_RE = re.compile(r'^(p[1-4])([a-z])?:\s*(.*)$')
CHAT_EVENTS = {'c', 'chat', 'j', 'join', 'n', 'name', 'l', 'leave'}
METADATA_EVENTS = {
    't:', 'player', 'teamsize', 'gametype', 'gen', 'tier', 'rule', 'rated',
    'start', 'turn', 'upkeep', 'inactive', 'inactiveoff', 'callback',
    'request', 'choice', 'teampreview', 'html', 'uhtml', 'uhtmlchange',
    'raw', 'error', 'warning', 'seed', 'inputlog', 'showteam', 'clearpoke',
    'poke',
}


def _number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _entity(value):
    """Split a protocol entity such as ``p1a: Lucario`` into seat and name."""
    match = ENTITY_RE.match(value)
    if not match:
        return None
    seat, slot, label = match.groups()
    return seat, slot or None, label.strip() or None


def _category(event):
    if event in CHAT_EVENTS:
        return 'chat'
    if event in METADATA_EVENTS:
        return 'metadata'
    if event.startswith('-') or event in {'move', 'switch', 'drag', 'replace', 'faint', 'win'}:
        return 'battle'
    return 'other'


def _parse_hp(value):
    """Parse exact Showdown HP notation; percentages cannot yield exact damage."""
    if not value:
        return None
    match = re.fullmatch(r'(\d+)/(\d+)', value.strip())
    if match:
        return tuple(map(int, match.groups()))
    return None


def _group_move_effects(actions, initial_hp=None):
    """Attach protocol outcomes to the move that caused them."""
    grouped = []
    pending_move = None
    players = {
        action['player_id']: action['player']
        for action in actions
        if action['event'] == 'player' and action['player_id'] and action['player']
    }
    effect_boundaries = {'turn', 'upkeep', 'switch', 'drag', 'replace', 'win'}
    hp_by_pokemon = dict(initial_hp or {})

    for action in actions:
        event = action['event']
        if event == 'move':
            action.update({
                'effects': [],
                'targets': [],
                'critical_hit': False,
                'missed': False,
                'damage': [],
            })
            if action['target_player_id'] or action['target_pokemon']:
                action['targets'].append({
                    'player': action['target_player'],
                    'player_id': action['target_player_id'],
                    'pokemon': action['target_pokemon'],
                })
            grouped.append(action)
            pending_move = action
            continue

        hp_key = (action['player_id'], (action['pokemon'] or '').casefold())
        hp_result = action['details'][1] if len(action['details']) > 1 else None
        if event in {'switch', 'drag', 'replace'} and len(action['details']) > 2:
            hp_result = action['details'][2]
        current_hp = _parse_hp(hp_result)
        damage_amount = None
        if event in {'switch', 'drag', 'replace'} and current_hp:
            hp_by_pokemon[hp_key] = current_hp

        if event in {'-damage', '-heal', '-sethp'} and current_hp:
            previous_hp = hp_by_pokemon.get(hp_key)
            if event == '-damage' and previous_hp and previous_hp[1] == current_hp[1]:
                damage_amount = max(0, previous_hp[0] - current_hp[0])
            hp_by_pokemon[hp_key] = current_hp

        if pending_move is not None and (event.startswith('-') or event == 'faint'):
            effect = {
                'event': event,
                'player': action['player'],
                'player_id': action['player_id'],
                'pokemon': action['pokemon'],
                'details': action['details'],
                'raw': action['raw'],
            }
            if event == '-damage':
                effect['damage_amount'] = damage_amount
            pending_move['effects'].append(effect)
            if event == '-miss':
                entities = [_entity(value) for value in action['details']]
                entities = [entity for entity in entities if entity]
                target_id, _, target_name = entities[-1] if entities else (None, None, None)
                target = {
                    'player': players.get(target_id),
                    'player_id': target_id,
                    'pokemon': target_name,
                }
                if (target_id or target_name) and target not in pending_move['targets']:
                    pending_move['targets'].append(target)
            elif action['player_id'] or action['pokemon']:
                target = {
                    'player': action['player'],
                    'player_id': action['player_id'],
                    'pokemon': action['pokemon'],
                }
                if target not in pending_move['targets']:
                    pending_move['targets'].append(target)
            if event == '-crit':
                pending_move['critical_hit'] = True
            elif event == '-miss':
                pending_move['missed'] = True
            elif event == '-damage':
                pending_move['damage'].append({
                    'player': action['player'],
                    'player_id': action['player_id'],
                    'pokemon': action['pokemon'],
                    'hp_result': hp_result,
                    'damage_amount': damage_amount,
                    'raw': action['raw'],
                })
            continue

        grouped.append(action)
        if event in effect_boundaries or (action['category'] == 'battle' and event != 'move'):
            pending_move = None

    for sequence, action in enumerate(grouped, start=1):
        action['sequence'] = sequence
    return grouped


def parse_battle_log(log):
    """Parse replay protocol lines into ordered records, grouping move outcomes."""
    if not isinstance(log, str):
        raise ValueError('Replay JSON field "log" must be a string')

    actions = []
    players = {}
    timestamp = None
    turn = None

    for line in log.splitlines():
        if not line or line == '|':
            continue
        parts = line.split('|')
        event = parts[1] if len(parts) > 1 else ''
        fields = parts[2:]

        # These markers establish context for subsequent battle events.
        if event == 't:' and fields:
            timestamp = _number(fields[0])
        elif event == 'turn' and fields:
            turn = _number(fields[0])

        player = player_id = pokemon = None
        target_player = target_player_id = target_pokemon = None
        move = message = None

        if event == 'player' and len(fields) >= 2:
            player_id, player = fields[0] or None, fields[1] or None
            if player_id and player:
                players[player_id] = player
        elif event in {'c', 'chat'} and len(fields) >= 2:
            player = fields[0] or None
            message = '|'.join(fields[1:])
        elif event in {'j', 'join', 'n', 'name', 'l', 'leave'} and fields:
            player = fields[0] or None
            message = '|'.join(fields[1:]) or None
        else:
            source_index = next((i for i, value in enumerate(fields) if _entity(value)), None)
            if source_index is not None:
                player_id, _, pokemon = _entity(fields[source_index])
                player = players.get(player_id)
                # Only battle action records use this entity label as a Pokémon.
                pokemon = pokemon if ':' in fields[source_index] and len(pokemon or '') else None
                if event == 'move' and len(fields) > source_index + 1:
                    move = fields[source_index + 1] or None
                    target_index = next(
                        (i for i in range(source_index + 2, len(fields)) if _entity(fields[i])),
                        None,
                    )
                    if target_index is not None:
                        target_player_id, _, target_pokemon = _entity(fields[target_index])
                        target_player = players.get(target_player_id)

        actions.append({
            'sequence': len(actions) + 1,
            'category': _category(event),
            'event': event or None,
            'timestamp': timestamp,
            'turn': turn,
            'player': player,
            'player_id': player_id,
            'pokemon': pokemon,
            'target_player': target_player,
            'target_player_id': target_player_id,
            'target_pokemon': target_pokemon,
            'move': move,
            'message': message,
            'details': fields,
            'raw': line,
        })
    first_turn_index = next(
        (i for i, action in enumerate(actions) if action['event'] == 'turn' and action['turn'] == 1),
        len(actions),
    )
    player_names = {name.casefold() for name in players.values() if name}
    prefix_chat = []
    for action in actions[:first_turn_index]:
        if action['event'] not in {'c', 'chat'} or not action['player']:
            continue
        author = action['player'].lstrip('~&@%+')
        if author.casefold() in player_names:
            prefix_chat.append(action)
    initial_hp = {}
    for action in actions[:first_turn_index]:
        if action['event'] not in {'switch', 'drag', 'replace'} or len(action['details']) < 3:
            continue
        hp = _parse_hp(action['details'][2])
        if hp:
            key = (action['player_id'], (action['pokemon'] or '').casefold())
            initial_hp[key] = hp
    return _group_move_effects(prefix_chat + actions[first_turn_index:], initial_hp)


def get_replay_log(replay_link):
    """Download the replay JSON and return its structured ordered actions."""
    response = requests.get(replay_link.rstrip('/') + '.json', timeout=replay_reader.REPLAY_TIMEOUT_SECONDS)
    response.raise_for_status()
    data = response.json()
    if 'log' not in data:
        raise ValueError('Replay JSON does not contain a "log" field')
    return parse_battle_log(data['log'])


def get_frigos(db_path, frigo_numbers):
    """Look up frigo numbers and replay URLs in the local database."""
    with sqlite3.connect(db_path) as conn:
        placeholders = ','.join('?' for _ in frigo_numbers)
        rows = conn.execute(
            'SELECT progr, replay_link FROM frigos '
            'WHERE progr IN ({}) ORDER BY progr'.format(placeholders),
            frigo_numbers,
        ).fetchall()
    found = {progr: replay_link for progr, replay_link in rows}
    missing = [number for number in frigo_numbers if number not in found]
    if missing:
        raise ValueError('Frigo non trovate nel database: {}'.format(', '.join(map(str, missing))))
    without_replay = [number for number in frigo_numbers if not found[number]]
    if without_replay:
        raise ValueError('Frigo senza replay link: {}'.format(', '.join(map(str, without_replay))))
    return [(number, found[number]) for number in sorted(set(frigo_numbers))]


def get_all_frigos(db_path):
    """Return every frigo with a non-empty replay URL, in frigo order."""
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT progr, replay_link FROM frigos "
            "WHERE replay_link IS NOT NULL AND TRIM(replay_link) != '' ORDER BY progr"
        ).fetchall()


def save_battle_actions(replay_link, output_path, frigo=None):
    """Fetch a replay and write a compact JSON document; return the action count."""
    actions = get_replay_log(replay_link)
    document = {
        'format': 'showdown-battle-actions-v1',
        'frigo': frigo,
        'replay': replay_link,
        'actions': actions,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(document, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    return len(actions)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--frigo', nargs='+', metavar='FRIGO',
                        help='frigo numbers to export, or "all" for every frigo with a replay link')
    source.add_argument('--replay-link', help='Showdown replay URL to export directly')
    parser.add_argument('--db', default=DEFAULT_DB_PATH, help='database path (default: frigo.db)')
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT_DIR,
                        help='destination directory (default: resources/battle_actions)')
    args = parser.parse_args()

    if args.frigo:
        if 'all' in args.frigo:
            if len(args.frigo) != 1:
                parser.error('"all" cannot be combined with frigo numbers')
            exports = get_all_frigos(args.db)
        else:
            try:
                frigo_numbers = [int(number) for number in args.frigo]
            except ValueError:
                parser.error('--frigo accepts integer numbers or the single value "all"')
            exports = get_frigos(args.db, frigo_numbers)
    else:
        exports = [(None, args.replay_link)]

    for frigo, replay_link in exports:
        filename = 'frigo_{}_actions.json'.format(frigo) if frigo is not None else '{}_actions.json'.format(
            replay_link.rstrip('/').rsplit('/', 1)[-1])
        output_path = args.output_dir / filename
        count = save_battle_actions(replay_link, output_path, frigo=frigo)
        print('Frigo {}: {} azioni salvate in {} ({:,} bytes)'.format(
            frigo if frigo is not None else replay_link, count, output_path, output_path.stat().st_size))


if __name__ == '__main__':
    main()
