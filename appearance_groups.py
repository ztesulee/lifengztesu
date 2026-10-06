"""Candidate clustering. Face identity wins; appearance never overrides a face conflict."""
import secrets
from collections import Counter
import numpy as np

def assign_appearance_groups(items, groups, model_id, use_faces):
    prototypes = {}

    def add(gid, observation):
        samples = prototypes.setdefault(gid, [])
        if len(samples) < 24:
            samples.append(observation)

    def ranking(observation, field):
        vector = observation.get(field)
        if vector is None:
            return []
        ranked = []
        for gid, samples in prototypes.items():
            values = [s[field] for s in samples if s.get(field) is not None]
            if values:
                ranked.append((max(float(np.asarray(vector) @ np.asarray(v)) for v in values), gid))
        return sorted(ranked, reverse=True)

    def fresh():
        gid = secrets.token_hex(6)
        prefix = '人物' if use_faces else '外观组'
        groups[gid] = {'name': f'{prefix} {len(groups) + 1:02d}', 'token': secrets.token_urlsafe(24), 'published': False}
        return gid

    # Confirmed one-person frames are reference examples. Do not attach every person
    # in a multi-person frame to the single manually selected album.
    for item in items:
        if item.get('analysis_model') != model_id:
            continue
        records = item.get('appearances', [])
        counts = Counter(o['frame'] for o in records)
        for observation in records:
            gid = observation.get('assigned_group')
            if item.get('reviewed'):
                gid = item['groups'][0] if len(item['groups']) == 1 and counts[observation['frame']] == 1 else None
            if gid in item['groups'] and gid in groups:
                add(gid, observation)

    for item in items:
        if item.get('analysis_model') != model_id or item.get('reviewed') or item.get('error') or item.get('groups'):
            continue
        assigned, used, uncertain, reasons = set(), {}, 0, set()
        for observation in item.get('appearances', []):
            face_scores = ranking(observation, 'face') if use_faces else []
            appearance_scores = ranking(observation, 'appearance')
            gid, reason = None, ''
            if face_scores and face_scores[0][0] >= .48 and (len(face_scores) == 1 or face_scores[0][0] - face_scores[1][0] >= .04):
                gid, reason = face_scores[0][1], '人脸'
            elif use_faces and observation.get('face') is not None and face_scores:
                if face_scores[0][0] >= .36:
                    uncertain += 1
                    continue
                # Different clear face: do not join an established face group based on clothes.
                if appearance_scores and appearance_scores[0][0] >= .80:
                    candidate = appearance_scores[0][1]
                    if not any(s.get('face') is not None for s in prototypes[candidate]):
                        if len(appearance_scores) == 1 or appearance_scores[0][0] - appearance_scores[1][0] >= .06:
                            gid, reason = candidate, '外观候选'
                if gid is None:
                    gid, reason = fresh(), '新人物候选'
            elif appearance_scores and appearance_scores[0][0] >= .80 and (len(appearance_scores) == 1 or appearance_scores[0][0] - appearance_scores[1][0] >= .06):
                gid, reason = appearance_scores[0][1], '外观候选'
            elif appearance_scores and appearance_scores[0][0] >= .60:
                uncertain += 1
                continue
            elif observation.get('appearance') is not None or (use_faces and observation.get('face') is not None):
                gid, reason = fresh(), '新人物候选' if use_faces else '新外观候选'
            if gid is None:
                continue
            frame_used = used.setdefault(observation['frame'], set())
            if gid in frame_used:
                uncertain += 1
                continue  # two distinct detected people in one frame cannot become one person
            observation['assigned_group'] = gid
            frame_used.add(gid)
            assigned.add(gid)
            reasons.add(reason)
            add(gid, observation)
        item['groups'] = sorted(assigned)
        item['note'] += '；OSNet 外观分析：' + ('、'.join(sorted(reasons)) if assigned else '未可靠匹配，请手动归类')
        if uncertain:
            item['note'] += f'；{uncertain} 个人物抽样存在冲突/歧义'
        item['note'] += '；服装/整体外观仅作为人物候选，需人工确认'
