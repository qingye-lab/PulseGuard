import { Alert, Spin } from "antd";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, PROJECT_STORAGE_KEY, setActiveProjectId } from "./api";
import type { Project } from "./types";

type ProjectContextValue = {
  projects: Project[];
  currentProject: Project;
  selectProject: (projectId: number) => void;
  refreshProjects: (preferredProjectId?: number) => Promise<void>;
};

const ProjectContext = createContext<ProjectContextValue | null>(null);

function requestedProjectIdFromLocation(): number | undefined {
  const value = new URLSearchParams(window.location.search).get("project_id");
  if (!value) return undefined;
  const projectId = Number(value);
  return Number.isInteger(projectId) && projectId > 0 ? projectId : undefined;
}

export function ProjectProvider({ children }: { children: ReactNode }) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [currentProject, setCurrentProject] = useState<Project | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const currentProjectIdRef = useRef<number | null>(null);

  const loadProjects = useCallback(async (preferredProjectId?: number) => {
    const nextProjects = await api.projects();
    if (!nextProjects.length) throw new Error("系统中没有可用项目");
    const storedId = currentProjectIdRef.current
      ? undefined
      : Number(window.localStorage.getItem(PROJECT_STORAGE_KEY));
    const desiredId = preferredProjectId || currentProjectIdRef.current || storedId;
    const selected =
      nextProjects.find((project) => project.id === desiredId) ||
      nextProjects.find((project) => project.status === "active") ||
      nextProjects[0];
    window.localStorage.setItem(PROJECT_STORAGE_KEY, String(selected.id));
    setActiveProjectId(selected.id);
    currentProjectIdRef.current = selected.id;
    setProjects(nextProjects);
    setCurrentProject(selected);
    setError("");
  }, []);

  useEffect(() => {
    loadProjects(requestedProjectIdFromLocation())
      .catch((reason: Error) => setError(reason.message || "项目加载失败"))
      .finally(() => setLoading(false));
  }, [loadProjects]);

  const value = useMemo<ProjectContextValue | null>(() => {
    if (!currentProject) return null;
    return {
      projects,
      currentProject,
      selectProject: (projectId) => {
        const selected = projects.find((project) => project.id === projectId);
        if (!selected || selected.id === currentProject.id) return;
        window.localStorage.setItem(PROJECT_STORAGE_KEY, String(selected.id));
        setActiveProjectId(selected.id);
        currentProjectIdRef.current = selected.id;
        setCurrentProject(selected);
      },
      refreshProjects: loadProjects
    };
  }, [currentProject, loadProjects, projects]);

  if (loading) {
    return (
      <div className="project-bootstrap-state">
        <Spin size="large" />
      </div>
    );
  }
  if (error || !value) {
    return (
      <div className="project-bootstrap-state">
        <Alert type="error" showIcon message="无法加载项目" description={error || "请检查数据库迁移状态"} />
      </div>
    );
  }
  return <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>;
}

export function useProject(): ProjectContextValue {
  const value = useContext(ProjectContext);
  if (!value) throw new Error("ProjectProvider is missing");
  return value;
}
