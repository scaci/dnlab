from dnlab_multinode.services import clab_apply_plan


def test_parse_recreated_nodes_from_containerlab_table():
    output = """
15:43:14 INFO Apply plan
╭─────────────────┬────────────────────────╮
│      Action     │         Details        │
├─────────────────┼────────────────────────┤
│ recreated nodes │ n1 (config drift: Env) │
╰─────────────────┴────────────────────────╯
"""

    entries = clab_apply_plan.parse_apply_plan(output)

    assert entries == [
        clab_apply_plan.ApplyPlanEntry(
            action="recreated nodes",
            details="n1 (config drift: Env)",
            nodes=("n1",),
        )
    ]


def test_parse_added_link_nodes_from_containerlab_table():
    output = """
15:43:29 INFO Apply plan
╭─────────────┬────────────────────╮
│    Action   │       Details      │
├─────────────┼────────────────────┤
│ added links │ n1:eth1 -- n2:eth1 │
╰─────────────┴────────────────────╯
"""

    entries = clab_apply_plan.parse_apply_plan(output)

    assert entries[0].action == "added links"
    assert entries[0].nodes == ("n1", "n2")


def test_recreate_violates_live_policy_but_not_recreate_policy():
    entries = [
        clab_apply_plan.ApplyPlanEntry(
            action="recreated nodes",
            details="n1 (config drift: Env)",
            nodes=("n1",),
        ),
        clab_apply_plan.ApplyPlanEntry(
            action="recreated nodes",
            details="vm1 (config drift: Env)",
            nodes=("vm1",),
        ),
    ]

    violations = clab_apply_plan.policy_violations(
        entries,
        {"n1": "live", "vm1": "recreate"},
    )

    assert [v.node for v in violations] == ["n1"]
    assert violations[0].action == "recreated nodes"


def test_restart_is_allowed_only_for_restart_policy():
    entries = [
        clab_apply_plan.ApplyPlanEntry(
            action="restarted nodes",
            details="ceos1 (config drift: Links)",
            nodes=("ceos1",),
        ),
        clab_apply_plan.ApplyPlanEntry(
            action="restarted nodes",
            details="vm1 (config drift: Links)",
            nodes=("vm1",),
        ),
        clab_apply_plan.ApplyPlanEntry(
            action="restarted nodes",
            details="linux1 (config drift: Links)",
            nodes=("linux1",),
        ),
    ]

    violations = clab_apply_plan.policy_violations(
        entries,
        {
            "ceos1": "restart",
            "vm1": "recreate",
            "linux1": "live",
        },
    )

    assert [(v.node, v.apply_mode) for v in violations] == [
        ("vm1", "recreate"),
        ("linux1", "live"),
    ]


def test_entries_serialize_and_summarize_for_state():
    entries = [
        clab_apply_plan.ApplyPlanEntry(
            action="added links",
            details="n1:eth1 -- n2:eth1",
            nodes=("n1", "n2"),
        )
    ]

    data = clab_apply_plan.entries_to_dicts(entries)

    assert data == [
        {
            "action": "added links",
            "details": "n1:eth1 -- n2:eth1",
            "nodes": ["n1", "n2"],
        }
    ]
    assert clab_apply_plan.dicts_summary(data) == "added links: n1:eth1 -- n2:eth1"
