import { Link } from "../router";

export function NotFoundPage(): React.JSX.Element {
  return (
    <div className="page narrow-page">
      <div className="empty-state large">
        <div className="empty-mark" aria-hidden="true">404</div>
        <h1>Страница не найдена</h1>
        <p>Возможно, ссылка относится к запуску, которого уже нет в runs-root.</p>
        <Link className="button button-primary" href="/">К обзору</Link>
      </div>
    </div>
  );
}
