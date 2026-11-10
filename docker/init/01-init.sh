#!/bin/bash
set -e

echo "Создание баз данных..."
psql --command="CREATE DATABASE deals_bigdata;"
psql --command="CREATE DATABASE dwh;"

echo "INIT: базы созданы"
