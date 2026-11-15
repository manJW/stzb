"""Verified current-role perspective; union zero never denotes a shared group."""
from observation_codec import digest

def perspective_id(profile):
    return digest([profile.get(k)for k in ('server','season','account_role_id','union_id','force_type')])[:24]

def context(profile):
    wanderer=profile.get('force_type')==1 or profile.get('is_wanderer')is True
    organized=str(profile.get('union_id')or'0')not in ('','0')
    role=profile.get('battle_role_id')
    return dict(context_id=perspective_id(profile),is_wanderer=wanderer,has_organization=organized,
                own_player_key='battle:'+str(role)if role else None,
                own_label='同聚义'if wanderer and organized else '自己'if not organized else '本盟',
                statistics_title='流浪军战绩'if wanderer else '个人战绩'if not organized else '同盟考勤',
                identity_label='流浪军'if wanderer else '普通势力',
                organization_label='聚义'if wanderer else '同盟',
                report_scope=('所属聚义可访问战报'if wanderer else '所属同盟可访问战报')if organized else '本人可访问战报')
