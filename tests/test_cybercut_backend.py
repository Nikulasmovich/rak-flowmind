from flowmind.editing.cybercut_backend import _approval_policy, stage_map


def test_flowmind_review_gates_map_to_cybercut_nodes():
    policy = _approval_policy(["copy", "storyboard", "pre_render"])

    assert policy.to_json() == {
        "mode": "hybrid",
        "gates": ["creative.brief", "creative.storyboard", "edit.plan"],
        "decided_by": "system:policy",
    }


def test_empty_review_gates_use_automatic_cybercut_policy():
    policy = _approval_policy([])

    assert policy.to_json()["mode"] == "auto"
    assert policy.to_json()["gates"] == []


def test_flowmind_progress_stages_follow_cybercut_dag_milestones():
    assert stage_map() == {
        "understand": "asset_analysis",
        "copy": "creative.brief",
        "script": "creative.script",
        "storyboard": "creative.storyboard",
        "render": "render.qc",
    }
