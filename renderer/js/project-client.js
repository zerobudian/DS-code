/* Project data operations kept separate from the chat/rendering lifecycle. */
class ProjectClient {
  constructor(request) { this.request = request; this.pending = false; }
  path(project, suffix = '') { return '/api/projects/' + encodeURIComponent(project) + suffix; }
  tasks(project) { return this.request(this.path(project, '/tasks')); }
  saveTasks(project, tasks, revision) {
    return this.request(this.path(project, '/tasks'), 'PUT', { tasks, revision });
  }
  pin(project, pinned) { return this.request(this.path(project), 'PUT', { pinned }); }
  rename(project, display_name) { return this.request(this.path(project), 'PUT', { display_name }); }
  files(project) { return this.request(this.path(project, '/files')); }
  file(project, file) {
    return this.request(this.path(project, '/file') + '?area=' + encodeURIComponent(file.area)
      + '&path=' + encodeURIComponent(file.path));
  }
  async upload(project, file) {
    if (file.size > 512 * 1024) throw new Error('每个附件最大 512 KiB / Maximum attachment size: 512 KiB');
    const buffer = await file.arrayBuffer();
    let binary = ''; const bytes = new Uint8Array(buffer);
    for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
    return this.request(this.path(project, '/uploads'), 'POST', { name: file.name, data: btoa(binary) });
  }
}
if (typeof module !== 'undefined') module.exports = { ProjectClient };
