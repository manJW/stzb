"""Reconnect verified login identities to archives made with legacy city IDs."""
import json
import zlib
from observation_codec import digest,packed


def resolve(db,profile):
    canonical=digest([profile.get(k)for k in ('server','season','account_role_id')])[:24]
    db.execute('''CREATE TABLE IF NOT EXISTS profile_aliases(canonical_id TEXT PRIMARY KEY,
        archive_id TEXT NOT NULL,evidence TEXT NOT NULL)''')
    saved=db.execute('SELECT archive_id FROM profile_aliases WHERE canonical_id=?',(canonical,)).fetchone()
    if saved:return saved[0]
    if db.execute('SELECT 1 FROM profiles WHERE id=?',(canonical,)).fetchone():return canonical
    # Login snapshots are already-owned local evidence. Match the legacy city
    # field as well as the private login user and numeric server/season; never
    # connect archives merely by name, union, or a shared map location.
    from installed_mode import logged_in_role
    candidates={}
    for row in db.execute('''SELECT m.id,m.profile_id,m.payload,p.data FROM messages m JOIN profiles p ON p.id=m.profile_id
        WHERE m.message_id=99991 ORDER BY m.first_seen DESC LIMIT 64'''):
        try:
            identity=logged_in_role(json.loads(zlib.decompress(row[2])));old=json.loads(row[3])
        except (ValueError,TypeError,zlib.error):continue
        if not identity or not identity.get('scope'):continue
        if (identity['user_id']!=str(profile.get('account_role_id'))or
                any(identity['scope'][k]!=profile.get(k)for k in ('server','season'))or
                identity['account_role_id']!=str(old.get('account_role_id'))or
                identity['account_name']!=old.get('account_name')):continue
        candidates[row[1]]=dict(message_id=row[0],user_id=identity['user_id'],scope=identity['scope'],method='verified_legacy_login')
    if len(candidates)!=1:return canonical
    selected,evidence=next(iter(candidates.items()))
    db.execute('INSERT INTO profile_aliases VALUES(?,?,?)',(canonical,selected,packed(evidence)))
    return selected
