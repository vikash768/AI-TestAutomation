"""A tiny React app written to a temp dir for parser tests."""
from pathlib import Path

FILES = {
    "package.json": '{"name":"mini","dependencies":{"react":"18","react-router-dom":"6"}}',
    "src/App.jsx": """
import { Route, Routes } from 'react-router-dom';
import { ProtectedRoute } from './routes/ProtectedRoute';
import Shell from './layout/Shell';
import Login from './pages/Login';
import Home from './pages/Home';
import Settings from './pages/Settings';
export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<ProtectedRoute />}>
        <Route element={<Shell />}>
          <Route path="/home" element={<Home />} />
          <Route path="/settings" element={<Settings />} />
        </Route>
      </Route>
    </Routes>
  );
}
""",
    "src/routes/ProtectedRoute.jsx": """
import { Navigate, Outlet } from 'react-router-dom';
export function ProtectedRoute() { const ok = false; if (!ok) return <Navigate to="/login" replace />; return <Outlet />; }
""",
    "src/layout/Shell.jsx": """
import { NavLink, Outlet } from 'react-router-dom';
import { Home, Cog } from 'icons';
const NAV_ITEMS = [{ to: '/home', icon: Home, label: 'Home' }, { to: '/settings', icon: Cog, label: 'Settings' }];
export default function Shell() {
  async function handleLogout() { await logout(); navigate('/login'); }
  return (<div>{NAV_ITEMS.map((item) => { const Icon = item.icon; return <NavLink key={item.to} to={item.to}><Icon className="x" />{item.label}</NavLink>; })}
    <button type="button" onClick={handleLogout}>Log out</button><Outlet /></div>);
}
""",
    "src/components/Button.jsx": """
import { forwardRef } from 'react';
export const Button = forwardRef(function Button({ variant, ...props }, ref) { return <button ref={ref} className="b" {...props} />; });
export function Field({ label, id, ...inputProps }) { return <div><label htmlFor={id}>{label}</label><input id={id} {...inputProps} /></div>; }
export function Modal({ open, children }) { if (!open) return null; return <div role="dialog"><button onClick={() => close()}>Close</button>{children}</div>; }
""",
    "src/pages/Login.jsx": """
import { Button, Field } from '../components/Button';
export default function Login() {
  const [show, setShow] = useState(false);
  async function handleSubmit(e) { e.preventDefault(); await login({ email, password }); navigate('/home'); }
  return (<form onSubmit={handleSubmit}>
    <label htmlFor="email">Email</label><input id="email" type="email" required onChange={(e) => setEmail(e.target.value)} />
    <button type="button" aria-label={show ? 'Hide password' : 'Show password'} onClick={() => setShow((v) => !v)}><Eye /></button>
    <Button type="submit" disabled={submitting}>{submitting ? 'Signing in…' : 'Sign in'}</Button>
  </form>);
}
""",
    "src/pages/Home.jsx": """
import { Link } from 'react-router-dom';
import { Button } from '../components/Button';
export default function Home() {
  const { isAdmin } = useAuth();
  function openEditor() { openDrawer({ title: 'Edit', content: <Editor onSave={async () => { await api.updateThing(1); }} /> }); }
  async function remove() { await api.deleteThing(1); }
  return (<div>
    <Link to="/settings"><Button>Go to settings</Button></Link>
    <Button onClick={openEditor}>Edit thing</Button>
    <Button onClick={remove}>Delete thing</Button>
    {isAdmin && <a href="https://example.com/help" target="_blank">Help</a>}
    <Button onClick={() => api.reactivate(1)}>Reactivate</Button>
  </div>);
}
""",
    "src/pages/Settings.jsx": """
const TABS = ['General', 'Email'];
const DANGER = 'Danger Zone';
export default function Settings() {
  const tabs = owner ? [...TABS, DANGER] : TABS;
  const [tab, setTab] = useState('General');
  return (<div>{tabs.map((t) => <button key={t} onClick={() => setTab(t)}>{t}</button>)}
    <button type="button" role="switch" aria-checked={on} aria-label="Enable thing" onClick={() => setOn(!on)} /></div>);
}
""",
    "src/api/things.js": """
import { request } from './client';
export function updateThing(id) { return request(URL, 'PATCH', `/api/things/${id}`); }
export function deleteThing(id) { return request(URL, 'DELETE', `/api/things/${id}`); }
export function reactivate(id) { return request(URL, 'POST', `/api/things/${id}/reactivate`); }
export function poll() { return setInterval(() => request(URL, 'GET', '/api/things'), 15000); }
""",
}


def write_app(root: Path) -> Path:
    for rel, content in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content.strip() + "\n", encoding="utf-8")
    return root
