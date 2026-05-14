@echo off
title Publicar Dashboard - Ojo con el Trend
color 0A

echo.
echo  ================================
echo   PUBLICANDO DASHBOARD EN INTERNET
echo  ================================
echo.

cd /d "C:\Users\santi\ojo-con-el-trend"

echo [1/3] Guardando cambios...
git add data/processed/negocio.db dashboard/app.py scripts/config.py

echo [2/3] Creando version nueva...
git commit -m "actualizar datos %date% %time%"

echo [3/3] Subiendo a internet...
git push

echo.
echo  ================================
echo   LISTO! En 30 segundos tu
echo   dashboard estara actualizado.
echo  ================================
echo.
pause
