import type {
  ApiKeyView,
  AuthMethodsView,
  HealthView,
  IssuedKeyView,
  MonitoringView,
  SandboxView,
  TemplateRecordView,
  TenantView,
  WhoamiView,
  WorkspaceListView,
  WorkspaceReadView,
  WorkspaceView,
} from "./types";

export class ControlPlaneError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, message: string, body: unknown) {
    super(message);
    this.name = "ControlPlaneError";
    this.status = status;
    this.body = body;
  }
}

export type TenantStatus = "active" | "suspended";

export interface TenantInput {
  id: string;
  display_name: string;
  max_workspaces: number;
  max_runtimes: number;
}

export interface TemplateInput {
  template_id: string;
  image: string;
  /** Defaults to '*' for a global template. */
  tenant_id?: string;
}

/** Every Control Plane endpoint used by either the live or development client. */
export interface ControlPlaneApi {
  authMethods(): Promise<AuthMethodsView>;
  whoami(tokenOverride?: string): Promise<WhoamiView>;
  logout(): Promise<void>;
  getHealth(): Promise<HealthView>;
  getMonitoring(): Promise<MonitoringView>;
  listSandboxes(): Promise<SandboxView[]>;
  listWorkspaces(): Promise<WorkspaceView[]>;
  deleteSandbox(id: string): Promise<void>;
  deleteWorkspace(id: string): Promise<void>;
  listWorkspaceFiles(id: string, path: string): Promise<WorkspaceListView>;
  /**
   * @param offset 1-based line to start from. Pass the previous page's
   *        `next_offset`, never `end_line + 1`: a hard-clipped line is skipped
   *        by File Service and the two differ exactly there.
   */
  readWorkspaceFile(id: string, path: string, offset?: number): Promise<WorkspaceReadView>;
  listTenants(): Promise<TenantView[]>;
  createTenant(input: TenantInput): Promise<void>;
  setTenantStatus(id: string, status: TenantStatus): Promise<void>;
  listApiKeys(tenantId: string): Promise<ApiKeyView[]>;
  issueApiKey(tenantId: string, label: string): Promise<IssuedKeyView>;
  revokeApiKey(keyId: string): Promise<void>;
  listTemplateIds(): Promise<string[]>;
  listTemplateRecords(): Promise<TemplateRecordView[]>;
  createTemplate(input: TemplateInput): Promise<void>;
  deleteTemplate(templateId: string, tenantId: string): Promise<void>;
}
