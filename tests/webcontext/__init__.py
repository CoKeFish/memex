# Paquete de tests de webcontext. El __init__.py evita la colisión de basename
# (test_config/test_cli/test_service también existen en tests/geo) bajo el import-mode
# `prepend` de pytest, que en la suite completa con -n auto da "import file mismatch".
