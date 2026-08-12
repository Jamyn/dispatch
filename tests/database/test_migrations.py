def test_alembic_env_disposes_its_engine(monkeypatch, session):
    """Alembic's env.py must dispose the engine it creates.

    Alembic's module-global context keeps the last migration's connection
    alive, so an undisposed pool leaves an idle backend open for the life of
    the process -- enough to make DROP DATABASE fail with ObjectInUse.
    """
    import sqlalchemy

    from dispatch import config
    from dispatch.database.manage import version_schema

    created = []
    real_create_engine = sqlalchemy.create_engine

    def spy(*args, **kwargs):
        engine = real_create_engine(*args, **kwargs)
        created.append(engine)
        return engine

    # env.py is exec'd fresh per alembic command, so its `from sqlalchemy
    # import create_engine` resolves to the spy.
    monkeypatch.setattr(sqlalchemy, "create_engine", spy)

    version_schema(script_location=config.ALEMBIC_CORE_REVISION_PATH)

    assert created, "alembic env.py created no engine; the spy did not take effect"
    leaked = [engine for engine in created if engine.pool.checkedin()]
    assert not leaked, f"{len(leaked)} alembic engine(s) left a connection in the pool"
