"""Frozen schema for revision 0001; intentionally independent of app models."""
import sqlalchemy as sa


def baseline_metadata(postgis=False):
    metadata = sa.MetaData()
    def text(name, size=36, nullable=False):
        return sa.Column(name, sa.String(size), nullable=nullable)
    def fk(name, table, nullable=False):
        return sa.Column(name, sa.String(36), sa.ForeignKey(f"{table}.id"), nullable=nullable, index=True)
    def json(name, nullable=False):
        return sa.Column(name, sa.JSON(), nullable=nullable)
    def number(name, nullable=False):
        return sa.Column(name, sa.Integer(), nullable=nullable)
    def timestamp(name="created_at", nullable=False):
        return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)
    def table(name, *columns, constraints=()):
        return sa.Table(name, metadata, sa.Column("id", sa.String(36), primary_key=True), *columns, *constraints)
    table("users", sa.Column("username", sa.String(80), nullable=False, unique=True, index=True),
          text("password_hash",255), text("role",30), sa.Column("is_active",sa.Boolean(),nullable=False), timestamp())
    table("projects", text("name",200), sa.Column("description",sa.Text()), fk("owner_id","users"), timestamp())
    table("project_members", fk("project_id","projects"), fk("user_id","users"), text("project_role",30),
          constraints=[sa.UniqueConstraint("project_id","user_id",name="uq_project_member")])
    table("datasets", fk("project_id","projects"), text("name",200), text("source_organization",200,True),
          sa.Column("capture_date",sa.Date()), timestamp("uploaded_at"), text("content_hash",64,True),
          text("original_filename",255,True), text("mime_type",120,True), sa.Column("raw_path",sa.Text()),
          text("declared_crs",120,True), text("normalized_crs",120,True), text("geometry_type",50,True),
          text("status",40), number("record_count"), number("normalized_count"), json("validation_report",True),
          json("metadata_json"), text("parent_dataset_id",36,True))
    spatial = []
    if postgis:
        from geoalchemy2 import Geometry
        spatial = [sa.Column("spatial_geometry", Geometry("GEOMETRY",srid=4326,spatial_index=True))]
    table("source_features", fk("dataset_id","datasets"), text("original_id",255,True), json("raw_attributes"),
          json("original_geometry",True), json("normalized_geometry",True), text("geometry_type",50,True),
          text("status",30), sa.Column("processing_reason",sa.Text()), *spatial)
    table("parcel_entities", fk("project_id","projects"), text("canonical_key",255,True), timestamp())
    table("match_proposals", fk("project_id","projects"), fk("left_feature_id","source_features",True),
          fk("right_feature_id","source_features",True), sa.Column("score",sa.Float(),nullable=False),
          text("score_type",40), text("status",40), number("candidate_rank",True), text("model_version",80), json("evidence"), timestamp())
    table("topology_conflicts", fk("project_id","projects"), fk("dataset_id","datasets",True), fk("feature_id","source_features",True),
          text("conflict_type",60), text("severity",20), sa.Column("description",sa.Text(),nullable=False), json("details"), text("status",30), timestamp())
    table("change_proposals", fk("project_id","projects"), text("change_type",50), fk("source_feature_id","source_features",True),
          fk("comparison_feature_id","source_features",True), json("before_geometry",True), json("after_geometry",True),
          json("before_attributes",True), json("after_attributes",True), sa.Column("area_delta",sa.Float()),
          sa.Column("boundary_change",sa.Boolean(),nullable=False), text("status",40), json("evidence"), timestamp())
    table("review_decisions", fk("project_id","projects"), text("target_type",40), text("target_id"), fk("actor_id","users"),
          text("decision",40), sa.Column("rationale",sa.Text(),nullable=False), timestamp())
    table("published_versions", fk("project_id","projects"), number("version_number"), fk("created_by","users"), text("status",30),
          json("lineage_manifest"), timestamp(), constraints=[sa.UniqueConstraint("project_id","version_number",name="uq_project_version")])
    spatial = [sa.Column("spatial_geometry", Geometry("GEOMETRY",srid=4326,spatial_index=True))] if postgis else []
    table("publication_features", fk("version_id","published_versions"), fk("source_feature_id","source_features"),
          fk("parcel_entity_id","parcel_entities",True), json("attributes"), json("geometry",True), json("lineage"), *spatial)
    table("jobs", fk("project_id","projects",True), text("job_type",60), text("status",20), text("idempotency_key",255,True),
          json("payload"), json("result",True), sa.Column("error",sa.Text()), fk("created_by","users",True), timestamp(),
          timestamp("started_at",True), timestamp("finished_at",True),
          constraints=[sa.UniqueConstraint("project_id","idempotency_key",name="uq_project_job_idempotency")])
    table("audit_events", fk("project_id","projects",True), fk("actor_id","users",True), text("action",100),
          text("target_type",40,True), text("target_id",36,True), json("details"), timestamp())
    table("parcel_source_links", fk("project_id","projects"), fk("parcel_entity_id","parcel_entities"), fk("source_feature_id","source_features"),
          fk("match_proposal_id","match_proposals",True), text("link_status",30), timestamp(),
          constraints=[sa.UniqueConstraint("parcel_entity_id","source_feature_id",name="uq_parcel_source_link")])
    table("schema_mappings", fk("project_id","projects"), fk("dataset_id","datasets"), number("version"), text("status",30),
          json("mapping"), json("source_fields"), fk("created_by","users"), timestamp(),
          constraints=[sa.UniqueConstraint("dataset_id","version",name="uq_dataset_schema_mapping_version")])
    return metadata
