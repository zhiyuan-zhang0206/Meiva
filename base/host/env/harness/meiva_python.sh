#!/bin/sh
exec "${MEIVA_PYTHON_EXECUTABLE:?Meiva interpreter path is required}" -P "$@"
