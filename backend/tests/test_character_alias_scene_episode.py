"""P1-8 regression: character alias & crowd type landing; episode description isolation."""
from datetime import datetime
from sqlalchemy.orm import sessionmaker
from app.core.database import make_engine
from app.models import Base, Character, Project, Scene, SceneEpisode, Shot, ids
from app.services.character_alias import build_alias_record, classify_character_type, split_entity_alias
from app.services.script_parse import apply_parse_result, normalize_scene_name


def _env(tmp_path):
    engine = make_engine(f'sqlite:///{tmp_path / "alias.db"}')
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _project(maker):
    s = maker()
    n = datetime.now()
    p = Project(id=ids.project_id(), name='alias test', aspect_ratio='9:16',
                creation_type='script', status='active', created_at=n, updated_at=n)
    s.add(p)
    s.commit()
    return s, p


def _result(chars, scenes):
    return {'characters': chars, 'props': [], 'prop_events': [], 'episodes': [], 'scenes': scenes}


def _shot(ep, seq, cid=None):
    return {'episode': ep, 'seq': seq, 'shot_kind': 'visual', 'duration': 3, 'character_id': cid}


def test_split_alias():
    r = split_entity_alias('张三（三哥/张三丰）')
    assert r == ('张三', ['三哥', '张三丰'])
    assert split_entity_alias('林晚') == ('林晚', [])
    assert split_entity_alias('') == ('', [])


def test_classify():
    assert classify_character_type('村民们') == 'crowd'
    assert classify_character_type('众人') == 'crowd'
    assert classify_character_type('林晚') == 'individual'
    assert classify_character_type('林晚', explicit='crowd') == 'crowd'
    assert build_alias_record('张三（三哥）') == ('张三', ['三哥'], 'individual')


def test_alias_and_crowd_landing(tmp_path):
    m = _env(tmp_path)
    s, p = _project(m)
    r = _result(
        [{'name': '张三（三哥/张三丰）', 'description': 'protagonist'},
         {'name': '村民们'},
         {'name': '林晚'}],
        [{'episode': 1, 'title': 'yard', 'time_note': 'morning',
          'description': 'morning light',
          'shots': [_shot(1, 1, cid='三哥')]}],
    )
    apply_parse_result(s, p, r, episode_start=1, episode_count=1)
    s.commit()
    cs = {c.name: c for c in s.query(Character).all()}
    z = cs['张三（三哥/张三丰）']
    assert z.aliases == ['三哥', '张三丰']
    assert z.character_type == 'individual'
    assert cs['村民们'].character_type == 'crowd'
    assert cs['林晚'].character_type == 'individual'
    shot = s.query(Shot).one()
    assert shot.character_id == z.id
    link = s.query(SceneEpisode).one()
    assert link.description == 'morning light'


def test_alias_merge_incremental(tmp_path):
    m = _env(tmp_path)
    s, p = _project(m)
    apply_parse_result(s, p, _result([{'name': '张三（三哥）'}], []),
                       episode_start=1, episode_count=1)
    s.commit()
    apply_parse_result(s, p, _result([{'name': '张三（张三丰）'}], []),
                       episode_start=2, episode_count=1)
    s.commit()
    cs = s.query(Character).all()
    assert len(cs) == 1
    assert cs[0].aliases == ['三哥', '张三丰']


def test_episode_desc_does_not_override_scene(tmp_path):
    m = _env(tmp_path)
    s, p = _project(m)
    title, tn = 'yard', 'morning'
    sc = Scene(id=ids.scene_id(), project_id=p.id, episode=1, seq=1, title=title,
               normalized_name=normalize_scene_name(title, tn),
               description='project base desc', time_note=tn)
    s.add(sc)
    s.commit()
    apply_parse_result(s, p, _result([], [{'episode': 2, 'title': title, 'time_note': tn,
                                            'description': 'ep2 specific desc',
                                            'shots': [_shot(2, 1)]}]),
                       episode_start=2, episode_count=1)
    s.commit()
    s.refresh(sc)
    assert sc.description == 'project base desc'
    links = s.query(SceneEpisode).filter_by(scene_id=sc.id).all()
    assert {(li.episode, li.description) for li in links} == {(2, 'ep2 specific desc')}
