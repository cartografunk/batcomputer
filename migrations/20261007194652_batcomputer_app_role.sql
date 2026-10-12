-- Keep Railway's database credentials separate from the project's postgres admin.
-- Activate LOGIN and set a strong password outside migration history when deploying.
CREATE ROLE batcomputer_app NOLOGIN;
GRANT USAGE ON SCHEMA batcomputer TO batcomputer_app;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA batcomputer TO batcomputer_app;

CREATE POLICY batcomputer_app_access ON batcomputer.conversations FOR ALL TO batcomputer_app USING (true) WITH CHECK (true);
CREATE POLICY batcomputer_app_access ON batcomputer.messages FOR ALL TO batcomputer_app USING (true) WITH CHECK (true);
CREATE POLICY batcomputer_app_access ON batcomputer.runs FOR ALL TO batcomputer_app USING (true) WITH CHECK (true);
CREATE POLICY batcomputer_app_access ON batcomputer.plans FOR ALL TO batcomputer_app USING (true) WITH CHECK (true);
CREATE POLICY batcomputer_app_access ON batcomputer.reviews FOR ALL TO batcomputer_app USING (true) WITH CHECK (true);
CREATE POLICY batcomputer_app_access ON batcomputer.file_versions FOR ALL TO batcomputer_app USING (true) WITH CHECK (true);
CREATE POLICY batcomputer_app_access ON batcomputer.traces FOR ALL TO batcomputer_app USING (true) WITH CHECK (true);
CREATE POLICY batcomputer_app_access ON batcomputer.validations FOR ALL TO batcomputer_app USING (true) WITH CHECK (true);
