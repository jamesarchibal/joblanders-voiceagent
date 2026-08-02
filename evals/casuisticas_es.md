# Casuísticas de prueba — Lander (voz) · 30 guiones

Guion para reproducir por **teléfono** (+34 930 485 117) las mismas 30 conversaciones
que corre el harness sin teléfono (`evals/run_actions.py`). 10 por categoría.

- Todas las entidades llevan prefijo **`TEST ·`** → se pueden limpiar en bloque después.
- Números de tel. inventados; correos `@*.example` (no existen a propósito).
- **Resultado esperado por categoría (tool que debe disparar):**
  - Ventas → `log_lead` (empresa en CRM Lab, `source=voice`, stage `Contactado`)
  - Agenda → `book_demo` (evento en calendario "Entrevistas")
  - Ticket → `create_ticket` (fila en `landeros_tickets`, `source=voice`)

Resultado del harness sin teléfono (2026-08-02): **ventas 8/10 · agenda 10/10 · ticket 10/10**.
Los 2 misses de ventas fueron por seguir pidiendo datos sin llegar a registrar el lead
(`TEST · Selecta Talent`, `TEST · GrupoLabora`).

---

## VENTAS (10) — objetivo: que registre el lead y ofrezca que le llame comercial

Guion: *"Hola, soy {contacto}, de {empresa}. {necesidad}. ¿Me podéis mandar
información y que me llame alguien de comercial?"* → *"De momento solo quiero info,
aún no me va bien agendar nada."*

| # | Empresa | Contacto | Necesidad |
|---|---------|----------|-----------|
| 1 | TEST · Voxtel Staffing | Marina Colomer | ETT 50 personas, quiere automatizar el screening de candidatos |
| 2 | TEST · Brío RRHH | Álvaro Nieto | consultora de selección, 20 recruiters, busca agentes de IA para criba |
| 3 | TEST · Talentia Global | Nuria Sáez | multinacional, alto volumen de vacantes, quiere IA para primer filtro |
| 4 | TEST · Empléate Ya | Diego Ferrer | portal de empleo regional, quiere un agente de voz para candidatos |
| 5 | TEST · Recluta Norte | Iratxe Bilbao | ETT industrial, 15 vacantes/mes, interesada en la suite de agentes |
| 6 | TEST · HR Partners SL | Sergio Márquez | outsourcing de RRHH, quiere entender qué automatiza joblanders.ai |
| 7 | TEST · Selecta Talent | Paula Rincón | headhunting tech, quiere agente que preentreviste candidatos |
| 8 | TEST · Andes Staffing | Tomás Vidal | staffing logística, mucho volumen estacional, pide info comercial |
| 9 | TEST · Nexo Empleo | Carla Osuna | agencia de empleo, quiere reducir tiempo de criba con IA |
| 10 | TEST · GrupoLabora | Hugo Prat | grupo de 3 ETTs, evalúa proveedores de IA para reclutamiento |

---

## AGENDA (10) — objetivo: que agende la demo en un hueco concreto con el correo

Guion: *"Hola, soy {contacto}, de {empresa}. Nos interesa joblanders.ai y queremos ver
una demo."* → *"¿Podría ser {hueco}? Mi correo es {email}. La empresa es {empresa}."*
→ *"Perfecto, confírmame ese hueco."*

| # | Empresa | Contacto | Email | Hueco pedido |
|---|---------|----------|-------|--------------|
| 1 | TEST · Cumbre Selección | Elena Rís | elena@cumbretest.example | el martes a las cuatro de la tarde |
| 2 | TEST · Ágora Talent | Marc Solà | marc@agoratest.example | el martes a las cinco y media |
| 3 | TEST · Delta Recruit | Sonia Vega | sonia@deltatest.example | el miércoles a las cuatro |
| 4 | TEST · Orbe RRHH | Pau Miralles | pau@orbetest.example | el miércoles a las cinco y media |
| 5 | TEST · Zenit Staffing | Lucía Bravo | lucia@zenittest.example | el jueves a las cuatro |
| 6 | TEST · Faro Empleo | Nacho Peña | nacho@farotest.example | el jueves a las cinco y media |
| 7 | TEST · Vértice Talent | Rita Colom | rita@verticetest.example | el viernes a las cuatro |
| 8 | TEST · Prisma RRHH | Gorka Ruiz | gorka@prismatest.example | el viernes a las cinco y media |
| 9 | TEST · Aurora Recruit | Vera Lima | vera@auroratest.example | el lunes que viene a las cuatro |
| 10 | TEST · Meridian Staffing | Bruno Gil | bruno@meridiantest.example | el lunes que viene a las cinco y media |

---

## TICKET (10) — cliente EXISTENTE con incidencia → objetivo: abrir ticket

Guion: *"Hola, soy {contacto}, de {empresa}, ya somos clientes. Tengo una incidencia:
{detalle}"* → *"Sí, es urgente para nosotros. Ábreme un ticket por favor. El asunto es:
{asunto}."* → *"Vale, gracias, dejadlo registrado."*

| # | Empresa | Contacto | Asunto | Detalle | Prioridad |
|---|---------|----------|--------|---------|-----------|
| 1 | TEST · Acme Logística | Laura Gómez | TEST · No puedo entrar a la plataforma | Error de login desde ayer, credenciales correctas | alta |
| 2 | TEST · Feroli Group | Andrés Mota | TEST · No recibo los candidatos por email | Las notificaciones de nuevos candidatos no llegan | media |
| 3 | TEST · Distribuciones Sur | Pilar Nadal | TEST · El agente de voz se cuelga | Al lanzar una llamada el agente se queda mudo a mitad | alta |
| 4 | TEST · TecnoBar SL | Raúl Ibáñez | TEST · Error al exportar informe | El CSV de candidatos sale vacío al exportar | media |
| 5 | TEST · Hostal Marina | Cristina Vera | TEST · No puedo cambiar mi plan | Intento subir de plan y da error 500 | media |
| 6 | TEST · Clima Norte | Jon Aguirre | TEST · Caída total del servicio | Nada carga, la plataforma está caída para todo el equipo | urgente |
| 7 | TEST · Panadería Rú | Marta Coll | TEST · Candidato duplicado | Un candidato aparece dos veces y no puedo borrarlo | baja |
| 8 | TEST · AutoPiezas SA | Fer Delgado | TEST · La integración con mi ATS falló | La sincronización con nuestro ATS dejó de funcionar hoy | alta |
| 9 | TEST · EduForma | Alba Ruano | TEST · Reclamación por cobro | Me habéis cobrado dos veces este mes, quiero que se revise | alta |
| 10 | TEST · Verde Jardín | Iván Sanz | TEST · No suena el agente al llamar | Los candidatos dicen que llaman y no contesta nadie | urgente |

---

## Dónde verificar el resultado

- **Ventas** → dashboard `/crm` (Lab CRM) · fila con `source=voice`, stage `Contactado`.
- **Agenda** → Google Calendar, calendario **"Entrevistas"**.
- **Ticket** → dashboard LanderOS → tab **Tickets** (tabla `landeros_tickets`).

## Limpieza (borrar todo lo `TEST ·`)

```sql
delete from crm_companies   where name    like 'TEST ·%';
delete from landeros_tickets where asunto like 'TEST ·%';
-- calendario: borrar a mano los eventos "TEST · Demo joblanders.ai — …" en Entrevistas
```
