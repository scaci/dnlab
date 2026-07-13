from dnlab_multinode.controllers.node import NodeLifecycleController


def test_restart_reuses_per_vd_stop_then_start():
    controller = object.__new__(NodeLifecycleController)
    calls = []
    expected = object()
    controller.stop = lambda node: calls.append(("stop", node))

    def start(node):
        calls.append(("start", node))
        return expected

    controller.start = start

    assert controller.restart("r1") is expected
    assert calls == [("stop", "r1"), ("start", "r1")]
