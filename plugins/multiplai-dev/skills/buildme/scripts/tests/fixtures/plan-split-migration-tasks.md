## 1. Store reports in their own table

Add the `reports` table with a Django migration in `app/migrations/0002_reports.py`.

- [ ] 1.1 Write the migration and the `Report` model in `app/models.py`

Interfaces:
- Produces: `class Report(models.Model)` in `app/models.py`
- Consumes: (none)

## 2. Pick the colour of the settings page

Add a colour picker to the settings page in `ui/settings.tsx`.

- [ ] 2.1 Render the picker and keep the chosen colour

Interfaces:
- Produces: `export function ColourPicker(props: { value: string }): JSX.Element`
- Consumes: (none)
