from vkit.verifiers.spec import BuildCheck


def test_declared_verifier_exports_can_be_imported():
    namespace = {}
    exec("from vkit.verifiers import *", namespace)
    assert namespace["BuildCheck"] is BuildCheck
