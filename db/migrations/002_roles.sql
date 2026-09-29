-- Application roles (idempotent; runs on every `make up` via the db-migrate service).
-- order_svc is deliberately NOT a superuser: when normal connection slots are exhausted, the
-- superuser_reserved_connections still let operators/exporters in, as in a real deployment.
-- reporting is a read-only role for ad-hoc/batch access to the orders data.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'order_svc') THEN
        CREATE ROLE order_svc LOGIN PASSWORD 'order_svc';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'reporting') THEN
        CREATE ROLE reporting LOGIN PASSWORD 'reporting';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE orders TO order_svc, reporting;
GRANT SELECT, INSERT, UPDATE ON orders TO order_svc;
GRANT SELECT ON orders TO reporting;
