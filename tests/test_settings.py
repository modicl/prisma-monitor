import pytest

from monitor.settings import Settings


def test_por_defecto_sirve_el_front():
    assert Settings.from_env({}).serve_front is True


@pytest.mark.parametrize("valor", ["false", "FALSE", "0", "no", "off", " false "])
def test_serve_front_apagado(valor):
    assert Settings.from_env({"SERVE_FRONT": valor}).serve_front is False


@pytest.mark.parametrize("valor", ["true", "True", "1", "yes", "on"])
def test_serve_front_encendido(valor):
    assert Settings.from_env({"SERVE_FRONT": valor}).serve_front is True


def test_serve_front_invalido_falla_con_mensaje_claro():
    with pytest.raises(ValueError, match="SERVE_FRONT"):
        Settings.from_env({"SERVE_FRONT": "quizas"})


def test_lee_el_resto_de_la_configuracion():
    s = Settings.from_env({"KAFKA_BOOTSTRAP_SERVERS": "kafka:9092", "STALL_MINUTES": "5", "PORT": "3999"})
    assert (s.kafka_bootstrap, s.stall_minutes, s.port) == ("kafka:9092", 5, 3999)


def test_entero_invalido_falla_con_mensaje_claro():
    with pytest.raises(ValueError, match="STALL_MINUTES"):
        Settings.from_env({"STALL_MINUTES": "mucho"})
