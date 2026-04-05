CREATE EXTENSION IF NOT EXISTS timescaledb;
SELECT 'CREATE DATABASE mlflow'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'mlflow')
\gexec
